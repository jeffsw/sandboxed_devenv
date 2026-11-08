"""`sandboxed-devenv backend ...`: what runs inside a backend VM (backend role).

`backend prepare --stage image` runs once while `image build` makes the base image, after the
wheel is installed: the backend's own files from the manifest (the sshd hardening, dev's
ssh_config), sshd restarted when its file changed, and ``/etc/sandboxed-devenv/image.yaml``
recording what the image is. `backend prepare --stage project` runs from a project's cloud-init
at first boot and is idempotent: the image files again, GitHub's SSH host keys, dev's git
identity, Claude's settings from ``project.yaml``, a key pair and alias stanza per listed
repository (no clone yet: the control side registers the keys on GitHub first, then runs
`backend repo add` again, which clones), and ``prepared.yaml``, which `backend status` turns
into ``ready``.

`backend repo add / rm / rekey / ls` are the VM side of the control machine's `repo` commands
and print JSON. A repository's key pair lives at ``~dev/.ssh/deploy_<owner>_<repo>``, its alias
stanza at ``~dev/.ssh/config.d/github.com-<owner>-<repo>.conf`` (dev's ssh_config includes that
directory), and its checkout at ``~dev/proj/<project>/<repo>`` (PLAN.md, "Projects and
repositories"). They run as dev over SSH or as root from prepare; files end up dev's either way.
"""

import dataclasses
import datetime
import json
import os
import pathlib
import pwd
import socket
import subprocess
import sys
import urllib.request

import yaml

from sandboxed_devenv import common, config, render, steps
from sandboxed_devenv.steps import CHANGED, FAILED, Outcome, outcome, read_text, run, sync_file

COMMAND = "backend prepare"
STAGES = ["image", "project"]
DEV_USER = "dev"
DEV_HOME = pathlib.Path("/home/dev")
SSHD_CONF = pathlib.Path("/etc/ssh/sshd_config.d/10-backend.conf")
SSH_HOST_KEY_GLOB = "ssh_host_*_key.pub"
GITHUB_META_URL = "https://api.github.com/meta"
IDE_DIR = pathlib.Path("/opt/jetbrains/idea")
IDE_PRODUCT_INFO = IDE_DIR / "product-info.json"
# What registerBackendLocationForGateway leaves behind on 2026.2: a symlink to the IDE directory
# under this directory, which Gateway's CustomInstallPathHelper reads at connection time (idea.log
# on the first image build, 8 November 2026). The daemon's rhizome database, used as the signal
# before, appears only after Gateway has connected once, so a fresh image never had it.
IDE_REGISTRATION = DEV_HOME / ".cache" / "JetBrains" / "RemoteDev" / "userProvidedDist"
GIT_SETTINGS = ("user.name", "user.email", "init.defaultBranch")
REPOSITORY_COMMENT = "# repository: "


@dataclasses.dataclass
class Paths:
    """Where a repository's pieces live, relative to a home; the defaults are dev's."""

    home: pathlib.Path = DEV_HOME
    user: str = DEV_USER
    group: str = DEV_USER

    @property
    def ssh_dir(self) -> pathlib.Path:
        retval = self.home / ".ssh"
        return retval

    @property
    def config_dir(self) -> pathlib.Path:
        retval = self.ssh_dir / "config.d"
        return retval

    @property
    def known_hosts(self) -> pathlib.Path:
        retval = self.ssh_dir / "known_hosts"
        return retval

    @property
    def proj_dir(self) -> pathlib.Path:
        retval = self.home / "proj"
        return retval

    def key_file(self, owner: str, repo: str) -> pathlib.Path:
        retval = self.ssh_dir / f"deploy_{owner}_{repo}"
        return retval

    def stanza_file(self, owner: str, repo: str) -> pathlib.Path:
        retval = self.config_dir / f"{alias(owner, repo)}.conf"
        return retval

    def checkout(self, project: str, repo: str) -> pathlib.Path:
        retval = self.proj_dir / project / repo
        return retval


def register(subparsers):
    backend = common.add_group(subparsers, "backend", "inside a backend VM: first boot, status, repositories (backend)")

    prepare = backend.add_parser(
        "prepare",
        help="the backend's files, GitHub host keys, git identity, Claude settings, a key per listed repository; needs root",
    )
    prepare.add_argument(
        "--stage",
        choices=STAGES,
        default="project",
        help="`image` while building the base image, `project` at a project's first boot (default)",
    )
    prepare.add_argument("--check", action="store_true", help="apply nothing; exit 1 if anything would change")
    prepare.set_defaults(handler=cmd_prepare)

    status = backend.add_parser(
        "status",
        help="cloud-init, the IDE backend, the package version, repositories, SSH host public keys, ready or not",
    )
    status.add_argument("--json", action="store_true", help="machine-readable, for the control side")
    status.set_defaults(handler=cmd_status)

    repo = common.add_group(backend, "repo", "the VM side of `repo`: key pairs, alias stanzas, checkouts")

    add = repo.add_parser("add", help="mint a key pair and alias stanza if missing, clone if missing, print the key as JSON")
    add.add_argument("repository", metavar="OWNER/REPO")
    add.add_argument("--no-clone", action="store_true", help="mint and bind the key but do not clone")
    add.set_defaults(handler=cmd_repo_add)

    rm = repo.add_parser("rm", help="delete the key pair and alias stanza; the checkout stays unless asked")
    rm.add_argument("repository", metavar="OWNER/REPO")
    rm.add_argument("--delete-checkout", action="store_true", help="also delete the checkout")
    rm.set_defaults(handler=cmd_repo_rm)

    rekey = repo.add_parser("rekey", help="a fresh key pair replacing the repository's, printed as JSON")
    rekey.add_argument("repository", metavar="OWNER/REPO")
    rekey.set_defaults(handler=cmd_repo_rekey)

    ls = repo.add_parser("ls", help="the repositories with their key fingerprints and checkouts")
    ls.add_argument("--json", action="store_true", help="machine-readable, for the control side")
    ls.set_defaults(handler=cmd_repo_ls)


# --- repositories ----------------------------------------------------------------------------


def split_repository(name: str) -> tuple[str, str]:
    """``owner/repo`` validated by the project model's pattern."""
    repository = config.Repository(name=name)
    retval = (repository.owner, repository.repo)
    return retval


def alias(owner: str, repo: str) -> str:
    """The ssh_config Host alias that binds a repository to its key."""
    retval = f"github.com-{owner}-{repo}"
    return retval


def clone_url(owner: str, repo: str) -> str:
    retval = f"git@{alias(owner, repo)}:{owner}/{repo}.git"
    return retval


def stanza_text(owner: str, repo: str, key_file: pathlib.Path) -> str:
    retval = (
        f"{REPOSITORY_COMMENT}{owner}/{repo}\n"
        f"Host {alias(owner, repo)}\n"
        f"    HostName github.com\n"
        f"    User git\n"
        f"    IdentityFile {key_file}\n"
        f"    IdentitiesOnly yes\n"
    )
    return retval


def run_as(paths: Paths, command: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run as the home's user: directly when that is who we are, else (as root) by switching."""
    me = pwd.getpwuid(os.geteuid())
    if me.pw_name == paths.user or os.geteuid() != 0:
        retval = run(command, **kwargs)
    else:
        account = pwd.getpwnam(paths.user)
        environment = {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        retval = run(command, user=account.pw_uid, group=account.pw_gid, extra_groups=[], env=environment, **kwargs)
    return retval


def mint_key(paths: Paths, key_file: pathlib.Path, comment: str) -> None:
    """ed25519, no passphrase (-N ""), with the comment (-C), at the file (-f); never leaves the VM."""
    steps.ensure_parents(key_file, pwd.getpwnam(paths.user).pw_uid, pwd.getpwnam(paths.user).pw_gid)
    completed = run_as(paths, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key_file)],
                       capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {completed.stderr.strip()}")


def fingerprint(public_key_file: pathlib.Path) -> str:
    completed = run(["ssh-keygen", "-lf", str(public_key_file)], capture_output=True)
    retval = completed.stdout.split()[1] if completed.returncode == 0 else "unknown"
    return retval


def repository_record(paths: Paths, owner: str, repo: str, project: str | None, created: bool = False) -> dict:
    key_file = paths.key_file(owner, repo)
    public = key_file.with_suffix(".pub")
    checkout = paths.checkout(project, repo) if project else None
    retdict = {
        "repository": f"{owner}/{repo}",
        "alias": alias(owner, repo),
        "key_file": str(key_file),
        "public_key": (read_text(public) or "").strip(),
        "fingerprint": fingerprint(public) if public.exists() else None,
        "created": created,
        "checkout": str(checkout) if checkout and checkout.is_dir() else None,
    }
    return retdict


def add_repository(paths: Paths, name: str, project: str | None, clone: bool, hostname: str) -> dict:
    """Idempotent: a missing key pair is minted, a missing stanza written, a missing checkout cloned
    when asked and a project is known."""
    owner, repo = split_repository(name)
    key_file = paths.key_file(owner, repo)
    created = False
    if not key_file.exists():
        mint_key(paths, key_file, f"{owner}/{repo} deploy key on {hostname}")
        created = True
    result = sync_file(paths.stanza_file(owner, repo), stanza_text(owner, repo, key_file).encode(),
                       paths.user, paths.group, 0o600, apply=True)
    if result.status == FAILED:
        raise RuntimeError(result.detail)
    if clone:
        if not project:
            raise RuntimeError("no project.yaml on this backend, so no checkout path; use --no-clone")
        checkout = paths.checkout(project, repo)
        if not checkout.exists():
            account = pwd.getpwnam(paths.user)
            steps.ensure_parents(checkout / "placeholder", account.pw_uid, account.pw_gid)
            completed = run_as(paths, ["git", "clone", clone_url(owner, repo), str(checkout)], capture_output=True)
            if completed.returncode != 0:
                raise RuntimeError(f"git clone failed: {completed.stderr.strip()}")
    retdict = repository_record(paths, owner, repo, project, created)
    return retdict


def remove_repository(paths: Paths, name: str, project: str | None, delete_checkout: bool) -> dict:
    owner, repo = split_repository(name)
    removed = []
    for path in (paths.key_file(owner, repo), paths.key_file(owner, repo).with_suffix(".pub"), paths.stanza_file(owner, repo)):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    checkout = paths.checkout(project, repo) if project else None
    if delete_checkout and checkout and checkout.exists():
        import shutil

        shutil.rmtree(checkout)
        removed.append(str(checkout))
    retdict = {
        "repository": f"{owner}/{repo}",
        "removed": removed,
        "checkout": str(checkout) if checkout and checkout.is_dir() else None,
    }
    return retdict


def rekey_repository(paths: Paths, name: str, project: str | None, hostname: str) -> dict:
    owner, repo = split_repository(name)
    key_file = paths.key_file(owner, repo)
    for path in (key_file, key_file.with_suffix(".pub")):
        if path.exists():
            path.unlink()
    retdict = add_repository(paths, name, project, clone=False, hostname=hostname)
    return retdict


def list_repositories(paths: Paths, project: str | None) -> list[dict]:
    retlist = []
    if not paths.config_dir.is_dir():
        return retlist
    for stanza in sorted(paths.config_dir.glob("*.conf")):
        name = None
        for line in (read_text(stanza) or "").splitlines():
            if line.startswith(REPOSITORY_COMMENT):
                name = line[len(REPOSITORY_COMMENT):].strip()
                break
        if not name or "/" not in name:
            continue
        owner, repo = name.split("/", 1)
        retlist.append(repository_record(paths, owner, repo, project))
    return retlist


def readable_project_config() -> config.ProjectConfig | None:
    """project.yaml when this user may read it (root only: it holds the token), else None."""
    retval = None
    if os.access(config.BACKEND_PROJECT_CONFIG, os.R_OK):
        retval = config.load_project_config()
    return retval


def prepared_marker() -> dict | None:
    """prepared.yaml, world-readable: the project's name and repositories as prepare last saw them."""
    retval = yaml.safe_load(read_text(config.BACKEND_PREPARED_MARKER) or "") or None
    return retval


def project_name() -> str | None:
    """From prepared.yaml, which dev can read, else from project.yaml when readable."""
    marker = prepared_marker()
    retval = marker.get("project") if marker else None
    if retval is None:
        project = readable_project_config()
        retval = project.project if project else None
    return retval


def print_json(data) -> None:
    print(json.dumps(data, indent=2))


def cmd_repo_add(args) -> int:
    try:
        print_json(add_repository(Paths(), args.repository, project_name(), clone=not args.no_clone,
                                  hostname=socket.gethostname()))
        retval = 0
    except (RuntimeError, ValueError, OSError) as error:
        print(f"sandboxed-devenv: backend repo add: {error}", file=sys.stderr)
        retval = 1
    return retval


def cmd_repo_rm(args) -> int:
    try:
        print_json(remove_repository(Paths(), args.repository, project_name(), args.delete_checkout))
        retval = 0
    except (RuntimeError, ValueError, OSError) as error:
        print(f"sandboxed-devenv: backend repo rm: {error}", file=sys.stderr)
        retval = 1
    return retval


def cmd_repo_rekey(args) -> int:
    try:
        print_json(rekey_repository(Paths(), args.repository, project_name(), socket.gethostname()))
        retval = 0
    except (RuntimeError, ValueError, OSError) as error:
        print(f"sandboxed-devenv: backend repo rekey: {error}", file=sys.stderr)
        retval = 1
    return retval


def cmd_repo_ls(args) -> int:
    records = list_repositories(Paths(), project_name())
    if args.json:
        print_json(records)
    elif not records:
        print("no repositories")
    else:
        for record in records:
            checkout = record["checkout"] or "not cloned"
            print(f"{record['repository']}: {record['fingerprint']} {record['alias']} {checkout}")
    retval = 0
    return retval


# --- prepare ----------------------------------------------------------------------------------


@dataclasses.dataclass
class Context(steps.Context):
    stage: str = "project"
    project: config.ProjectConfig | None = None
    paths: Paths = dataclasses.field(default_factory=Paths)


def step_files(ctx: Context) -> list[Outcome]:
    """The backend's files from the manifest: image-stage files at both stages, project-stage files
    only once project.yaml is there."""
    wanted = {"image"}
    if ctx.stage == "project":
        wanted.add("project")
    retlist = []
    for name, entry in render.load_manifest()["backend"].items():
        if entry.is_cloudinit or entry.stage not in wanted:
            continue
        try:
            content = render.render_entry("backend", name, entry, ctx.values)
            dest = render.rendered_dest(entry, ctx.values)
        except render.RenderError as error:
            retlist.append(Outcome(f"file {entry.dest}", FAILED, str(error)))
            continue
        result = sync_file(dest, content.encode(), entry.owner, entry.group, entry.mode_bits, ctx.apply)
        if result.status == CHANGED:
            ctx.changed.add(dest)
        retlist.append(result)
    return retlist


def step_sshd(ctx: Context) -> list[Outcome]:
    retlist = [outcome(ctx, "sshd", "systemctl restart ssh, since its config changed", SSHD_CONF in ctx.changed,
                       lambda: run(["systemctl", "restart", "ssh"], check=True), "config unchanged")]
    return retlist


def ide_backend_report() -> dict:
    info = None
    text = read_text(IDE_PRODUCT_INFO)
    if text:
        try:
            data = json.loads(text)
            info = {"name": data.get("name"), "version": data.get("version"), "build": data.get("buildNumber")}
        except json.JSONDecodeError:
            info = {"name": None, "version": None, "build": None}
    retdict = {"installed": info, "registered": ide_registered()}
    return retdict


def ide_registered(registration_dir: pathlib.Path = IDE_REGISTRATION, ide_dir: pathlib.Path = IDE_DIR) -> bool:
    """True when a link under the registration directory points at the IDE directory."""
    retval = False
    if registration_dir.is_dir():
        for entry in registration_dir.iterdir():
            try:
                if entry.is_symlink() and entry.resolve() == ide_dir.resolve():
                    retval = True
                    break
            except OSError:
                pass
    return retval


def image_marker_content() -> dict:
    versions = config.load_versions()
    ide = ide_backend_report()["installed"] or {}
    retdict = {
        "package_version": common.package_version(),
        "ide_backend_version": ide.get("version"),
        "ubuntu_image_serial": versions.ubuntu.image_serial,
    }
    return retdict


def write_yaml(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    path.chmod(0o644)


def step_image_marker(ctx: Context) -> list[Outcome]:
    wanted = image_marker_content()
    current = yaml.safe_load(read_text(config.BACKEND_IMAGE_MARKER) or "") or {}
    differs = {key: current.get(key) for key in wanted} != wanted
    detail = ", ".join(f"{key} {value}" for key, value in wanted.items())
    retlist = [outcome(ctx, f"{config.BACKEND_IMAGE_MARKER}", detail, differs,
                       lambda: write_yaml(config.BACKEND_IMAGE_MARKER,
                                          dict(wanted, built=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))),
                       detail)]
    return retlist


def github_host_keys() -> list[str]:
    with urllib.request.urlopen(GITHUB_META_URL, timeout=30) as response:
        meta = json.load(response)
    retlist = []
    for key in meta["ssh_keys"]:
        retlist.append(f"github.com {key}")
    return retlist


def step_github_host_keys(ctx: Context) -> list[Outcome]:
    """GitHub's SSH host keys from its published metadata, so the first clone trusts nothing on
    first use and meets no prompt."""
    known = read_text(ctx.paths.known_hosts) or ""
    present = "github.com ssh-ed25519" in known

    def append():
        lines = github_host_keys()
        existing = read_text(ctx.paths.known_hosts) or ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        content = existing + "\n".join(lines) + "\n"
        result = sync_file(ctx.paths.known_hosts, content.encode(), ctx.paths.user, ctx.paths.group, 0o600, apply=True)
        if result.status == FAILED:
            raise RuntimeError(result.detail)

    retlist = [outcome(ctx, "github host keys", f"append GitHub's keys from {GITHUB_META_URL} to {ctx.paths.known_hosts}",
                       not present, append, "present")]
    return retlist


def git_config_value(ctx: Context, key: str) -> str | None:
    completed = run_as(ctx.paths, ["git", "config", "--global", key], capture_output=True)
    retval = completed.stdout.strip() if completed.returncode == 0 else None
    return retval


def step_git_identity(ctx: Context) -> list[Outcome]:
    wanted = {
        "user.name": ctx.project.git.user_name,
        "user.email": ctx.project.git.user_email,
        "init.defaultBranch": "main",
    }
    retlist = []
    for key, value in wanted.items():
        current = git_config_value(ctx, key)

        def set_value(key=key, value=value):
            completed = run_as(ctx.paths, ["git", "config", "--global", key, value], capture_output=True)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip())

        retlist.append(outcome(ctx, f"git {key}", f"{value!r} (was {current!r})", current != value, set_value, repr(value)))
    return retlist


def step_repositories(ctx: Context) -> list[Outcome]:
    """A key pair and stanza per listed repository, no clone: the keys get registered first."""
    retlist = []
    hostname = socket.gethostname()
    for repository in ctx.project.repositories:
        key_file = ctx.paths.key_file(repository.owner, repository.repo)
        stanza = ctx.paths.stanza_file(repository.owner, repository.repo)
        differs = not (key_file.exists() and stanza.exists())
        retlist.append(outcome(ctx, f"repository {repository.name}", f"key pair {key_file} and alias {alias(repository.owner, repository.repo)}",
                               differs, lambda name=repository.name: add_repository(ctx.paths, name, ctx.project.project, False, hostname),
                               f"key {fingerprint(key_file.with_suffix('.pub')) if key_file.exists() else 'unknown'}"))
    return retlist


def step_prepared_marker(ctx: Context) -> list[Outcome]:
    """World-readable, so dev's `backend status` and `repo` commands know the project without the token."""
    wanted = {
        "project": ctx.project.project,
        "repositories": [repository.name for repository in ctx.project.repositories],
        "package_version": common.package_version(),
    }
    current = prepared_marker() or {}
    differs = {key: current.get(key) for key in wanted} != wanted
    retlist = [outcome(ctx, f"{config.BACKEND_PREPARED_MARKER}", f"project {ctx.project.project}", differs,
                       lambda: write_yaml(config.BACKEND_PREPARED_MARKER,
                                          dict(wanted, prepared=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))),
                       f"project {ctx.project.project}")]
    return retlist


IMAGE_STEPS = [step_files, step_sshd, step_image_marker]
PROJECT_STEPS = [step_files, step_sshd, step_github_host_keys, step_git_identity, step_repositories, step_prepared_marker]


def cmd_prepare(args) -> int:
    command = f"{COMMAND} --stage {args.stage}"
    if not common.linux_only(command):
        retval = 1
        return retval
    if os.geteuid() != 0:
        print(f"sandboxed-devenv: `{command}` needs root: sudo sandboxed-devenv {command}", file=sys.stderr)
        retval = 1
        return retval
    values = render.default_values()
    project = None
    if args.stage == "project":
        project = config.load_project_config()
        if project is None:
            print(f"sandboxed-devenv: no {config.BACKEND_PROJECT_CONFIG}; this VM is not a project's backend "
                  "(the project cloud-init writes it)", file=sys.stderr)
            retval = 1
            return retval
        values.update(render.project_values(project, socket.gethostname()))
        plan = PROJECT_STEPS
    else:
        plan = IMAGE_STEPS
    ctx = Context(apply=not (args.dry_run or args.check), values=values, command=command, stage=args.stage, project=project)
    outcomes = []
    for step in plan:
        for result in step(ctx):
            outcomes.append(result)
            print(result.line(command), flush=True)
    counts = steps.tally(outcomes)
    mode = "check" if args.check else "dry run" if args.dry_run else "prepare"
    print(steps.summary_line(command, mode, counts))
    retval = steps.exit_status(counts, args.check)
    return retval


# --- status -----------------------------------------------------------------------------------


def cloud_init_status() -> dict:
    retdict = {"status": "unknown", "detail": ""}
    try:
        completed = run(["cloud-init", "status", "--format", "json"], capture_output=True)
    except FileNotFoundError:
        retdict = {"status": "absent", "detail": "cloud-init is not installed"}
        return retdict
    if completed.returncode in (0, 1, 2) and completed.stdout.strip().startswith("{"):
        try:
            data = json.loads(completed.stdout)
            retdict = {"status": data.get("extended_status") or data.get("status") or "unknown", "detail": data.get("detail", "")}
        except json.JSONDecodeError:
            pass
    elif completed.returncode == 127 or "not found" in completed.stderr:
        retdict = {"status": "absent", "detail": "cloud-init is not installed"}
    else:
        for line in completed.stdout.splitlines():
            if line.startswith("status:"):
                retdict["status"] = line.split(":", 1)[1].strip()
    return retdict


def ssh_host_keys() -> list[str]:
    retlist = []
    for path in sorted(pathlib.Path("/etc/ssh").glob(SSH_HOST_KEY_GLOB)):
        text = read_text(path)
        if text:
            retlist.append(" ".join(text.split()[:2]))
    return retlist


def status_report() -> dict:
    project = readable_project_config()
    prepared = prepared_marker()
    name = project_name()
    cloud_init = cloud_init_status()
    image = yaml.safe_load(read_text(config.BACKEND_IMAGE_MARKER) or "") or None
    project_report = None
    if config.BACKEND_PROJECT_CONFIG.exists() or prepared:
        if project:
            repositories = [repository.name for repository in project.repositories]
        elif prepared:
            repositories = prepared.get("repositories")
        else:
            repositories = None
        project_report = {"name": name, "repositories": repositories, "prepared": prepared}
    retdict = {
        "hostname": socket.gethostname(),
        "package_version": common.package_version(),
        "cloud_init": cloud_init,
        "image": image,
        "project": project_report,
        "ide_backend": ide_backend_report(),
        "repositories": list_repositories(Paths(), name),
        "ssh_host_keys": ssh_host_keys(),
        "ready": cloud_init["status"].endswith("done") and prepared is not None,
    }
    return retdict


def cmd_status(args) -> int:
    report = status_report()
    if args.json:
        print_json(report)
    else:
        print(f"{report['hostname']}: sandboxed-devenv {report['package_version']}, ready: {report['ready']}")
        print(f"cloud-init: {report['cloud_init']['status']} {report['cloud_init']['detail']}".rstrip())
        print(f"image: {report['image'] or 'no image.yaml (not built by image build)'}")
        project = report["project"]
        if project:
            print(f"project: {project['name'] or 'unknown (project.yaml unreadable, not yet prepared)'}, "
                  f"prepared: {bool(project['prepared'])}, repositories: {', '.join(project['repositories'] or []) or 'none'}")
        else:
            print("project: none (no project.yaml)")
        ide = report["ide_backend"]
        installed = ide["installed"]
        if installed:
            print(f"ide backend: {installed['name']} {installed['version']}, registered for Gateway: {ide['registered']}")
        else:
            print(f"ide backend: not installed, registered for Gateway: {ide['registered']}")
        if report["repositories"]:
            for record in report["repositories"]:
                print(f"repository {record['repository']}: {record['fingerprint']} {record['checkout'] or 'not cloned'}")
        else:
            print("repositories: none")
        print(f"ssh host keys: {len(report['ssh_host_keys'])}")
    retval = 0
    return retval
