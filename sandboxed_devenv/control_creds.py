"""Repositories and their credentials: `repo ...` and `creds ...` (control role, M3).

One deploy key pair per repository lives inside the backend, minted there by `backend repo add`
and never leaving it. This module registers the public half on GitHub through `gh api`, records
the key's id and state in the inventory, revokes it before anything is deleted, rotates it, and
audits the inventory against what GitHub holds. The same calls serve `new` (register and clone
each `--repo`), `rm` (revoke every key first) and `clone` (register the copy's fresh keys).

GitHub calls go through `gh api` rather than `gh repo deploy-key`, because the API returns the
new key's id and the list as JSON; `gh` supplies the authentication. A failed revocation leaves
the entry `pending-revoke` for `creds audit` to retry, and a backend whose keys could not all be
revoked keeps its inventory record, marked removed, until they are.

Titles on GitHub start with ``sandboxed-devenv``, so `creds audit --fix` deletes only unclaimed
keys this tool made; a key with any other title is reported and left alone. Keys registered by
hand at M2 are adopted when their text matches an inventory entry.

The two key pairs every VM creation needs are minted here too: the control key (the control
machine into desk and every backend) and the sandbox key (gw into the backends only). Before the
split (PLAN.md M3) one pair, ``gw_ed25519``, served both; found, it becomes the control key, since
it is what the control side has been logging in with, and a sandbox key is minted beside it.
"""

import dataclasses
import datetime
import hashlib
import json
import pathlib
import shutil
import socket
import subprocess
import sys

import pydantic

from sandboxed_devenv import common, config, control_inventory, remote
from sandboxed_devenv.control_inventory import (
    ACTIVE,
    PENDING_REVOKE,
    UNREGISTERED,
    CredentialRecord,
    InventoryError,
    ProjectRecord,
    RepositoryRecord,
)
from sandboxed_devenv.remote import RemoteError

ALL_REPOSITORIES = "ALL"
CONTROL_KEY_COMMENT_USER = "control"
SANDBOX_KEY_COMMENT_USER = "gw"
GH = "gh"
TITLE_PREFIX = "sandboxed-devenv "
CLAUDE_TOKEN_KIND = "claude-token"
CLAUDE_SETTINGS_PATH = "/home/dev/.claude/settings.json"
CLAUDE_TOKEN_REVOKE = ("claude.ai: Settings, Privacy, log out everywhere; then `claude setup-token` into the secrets "
                       "directory and `creds rotate PROJECT --claude-token`")
ERRORS = (RemoteError, InventoryError, config.SecretError, OSError)


class GitHubError(RemoteError):
    pass


class GitHubNotFound(GitHubError):
    pass


# --- the control and sandbox keys ------------------------------------------------------------------


def mint_key(runner: common.Runner, path: pathlib.Path, comment: str) -> None:
    """ed25519, no passphrase (-N ""), with the comment (-C), at the file (-f)."""
    completed = runner.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)])
    if completed.returncode != 0:
        raise remote.RemoteError(f"ssh-keygen failed with status {completed.returncode}")


def ensure_keys(runner: common.Runner | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    """The control and sandbox key pairs in the secrets directory, minted on first use. A legacy
    ``gw_ed25519`` pair becomes the control key (renamed), and the first sandbox key minted next to
    it means desk and existing backends still hold the old arrangement until re-provisioned, which
    the message says."""
    runner = runner or common.Runner()
    control = remote.control_key_path()
    sandbox = remote.sandbox_key_path()
    secrets = control.parent
    secrets.mkdir(parents=True, exist_ok=True)
    secrets.chmod(0o700)
    legacy = secrets / config.LEGACY_RIG_KEY_NAME
    if not control.is_file() and legacy.is_file():
        legacy.rename(control)
        if legacy.with_suffix(".pub").is_file():
            legacy.with_suffix(".pub").rename(control.with_suffix(".pub"))
        print(f"{legacy.name} renamed to {control.name}: it is the key the control side logs in with; a separate sandbox "
              "key for gw follows (PLAN.md M3, the key split)", flush=True)
    hostname = socket.gethostname()
    if not control.is_file():
        mint_key(runner, control, f"{CONTROL_KEY_COMMENT_USER}@{hostname}")
        print(f"control key pair created: {control}; the control side logs into every VM made from now on with it", flush=True)
    if not sandbox.is_file():
        mint_key(runner, sandbox, f"{SANDBOX_KEY_COMMENT_USER}@{hostname}")
        print(f"sandbox key pair created: {sandbox}; backends made from now on let gw in with it. Desk needs it as "
              "/home/gw/.ssh/vm_ed25519: `sandboxed-devenv client-host provision` puts it there, and a backend made "
              "before this is reachable by gw again after `rm` and `new`", flush=True)
    retval = (control, sandbox)
    return retval


def public_key_of(path: pathlib.Path) -> str:
    """The public half, one line, for cloud-init's ssh_authorized_keys."""
    public = path.with_suffix(".pub")
    if not public.is_file():
        raise remote.RemoteError(f"no public key at {public} beside {path.name}")
    retval = public.read_text().strip()
    return retval


def control_public_key() -> str:
    retval = public_key_of(remote.control_key_path())
    return retval


def sandbox_public_key() -> str:
    retval = public_key_of(remote.sandbox_key_path())
    return retval


def seed_key_values(dry_run: bool, runner: common.Runner) -> dict[str, str]:
    """``CONTROL_PUBKEY`` and ``SANDBOX_PUBKEY`` for a seed; on a dry run, whatever exists or a
    note, without minting anything."""
    if dry_run:
        retdict = {}
        for name, path in (("CONTROL_PUBKEY", remote.control_key_path()), ("SANDBOX_PUBKEY", remote.sandbox_key_path())):
            retdict[name] = public_key_of(path) if path.with_suffix(".pub").is_file() else "ssh-ed25519 (minted on the real run)"
    else:
        ensure_keys(runner)
        retdict = {"CONTROL_PUBKEY": control_public_key(), "SANDBOX_PUBKEY": sandbox_public_key()}
    return retdict


def key_fingerprint(public_key_file: pathlib.Path) -> str | None:
    completed = subprocess.run(["ssh-keygen", "-lf", str(public_key_file)], capture_output=True, text=True)
    retval = completed.stdout.split()[1] if completed.returncode == 0 and completed.stdout.split() else None
    return retval


# --- GitHub ------------------------------------------------------------------------------------------


def key_text(public_key: str) -> str:
    """``type base64``, as GitHub stores a key: the comment dropped."""
    parts = public_key.split()
    retval = " ".join(parts[:2]) if len(parts) >= 2 else public_key.strip()
    return retval


def key_title(project: str) -> str:
    retval = f"{TITLE_PREFIX}{project}"
    return retval


def require_gh() -> str:
    retval = shutil.which(GH)
    if not retval:
        raise GitHubError("the GitHub CLI `gh` is not on the PATH; install it (`brew install gh`) and run `gh auth login`")
    return retval


@dataclasses.dataclass
class GitHub:
    """Deploy keys through `gh api`; the runner's dry run prints the calls and returns nothing."""

    runner: common.Runner = dataclasses.field(default_factory=common.Runner)

    def api(self, method: str, path: str, body: dict | None = None):
        command = [GH, "api", "--method", method, path]
        kwargs = {"capture_output": True}
        if body is not None:
            command += ["--input", "-"]
            kwargs["input"] = json.dumps(body)
        completed = self.runner.run(command, **kwargs)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            if "HTTP 404" in detail:
                raise GitHubNotFound(f"gh api {method} {path}: {detail.splitlines()[-1] if detail else 'not found'}")
            raise GitHubError(f"gh api {method} {path} failed with status {completed.returncode}: "
                              f"{detail[-300:] or 'no detail'}")
        text = (completed.stdout or "").strip()
        retval = json.loads(text) if text else None
        return retval

    def authenticated(self) -> bool:
        require_gh()
        completed = self.runner.run([GH, "auth", "status"], capture_output=True)
        retval = completed.returncode == 0
        return retval

    def list_deploy_keys(self, repository: str) -> list[dict]:
        retval = self.api("GET", f"repos/{repository}/keys?per_page=100") or []
        return retval

    def add_deploy_key(self, repository: str, public_key: str, title: str, read_only: bool) -> dict:
        retval = self.api("POST", f"repos/{repository}/keys",
                          {"title": title, "key": key_text(public_key), "read_only": read_only})
        if retval is None:
            retval = {"id": 0, "key": key_text(public_key), "title": title, "read_only": read_only}
        return retval

    def delete_deploy_key(self, repository: str, key_id: int) -> bool:
        """True when deleted, False when GitHub no longer had it."""
        try:
            self.api("DELETE", f"repos/{repository}/keys/{key_id}")
            retval = True
        except GitHubNotFound:
            retval = False
        return retval


def find_key(listed: list[dict], public_key: str | None) -> dict | None:
    retval = None
    if public_key:
        wanted = key_text(public_key)
        for item in listed:
            if key_text(item.get("key", "")) == wanted:
                retval = item
                break
    return retval


# --- registration and revocation ------------------------------------------------------------------


def register_key(github: GitHub, record: ProjectRecord, repository: RepositoryRecord) -> str:
    """The repository's minted key onto GitHub, or adopted when the same key is already there;
    the record is updated in place. Returns a line for the user."""
    if not repository.public_key:
        raise GitHubError(f"{repository.name}: the backend reported no public key to register")
    read_only = repository.access == "read"
    existing = find_key(github.list_deploy_keys(repository.name), repository.public_key)
    if existing is not None:
        repository.github_key_id = int(existing["id"])
        repository.title = existing.get("title")
        retval = f"{repository.name}: deploy key already on GitHub as id {existing['id']} ({existing.get('title')}); adopted"
        if bool(existing.get("read_only", True)) != read_only:
            retval += f"; it is {'read-only' if existing.get('read_only') else 'read-write'} where {repository.access} was asked"
    else:
        created = github.add_deploy_key(repository.name, repository.public_key, key_title(record.project), read_only)
        repository.github_key_id = int(created.get("id") or 0)
        repository.title = created.get("title") or key_title(record.project)
        retval = f"{repository.name}: deploy key registered on GitHub as id {repository.github_key_id} ({repository.access})"
    repository.state = ACTIVE
    repository.registered = control_inventory.now()
    return retval


def revoke_key(github: GitHub, repository: RepositoryRecord) -> tuple[bool, str]:
    """Delete the repository's key on GitHub, by id or by its text when it was registered by
    hand. On failure the entry becomes pending-revoke. Returns (revoked, line)."""
    try:
        key_id = repository.github_key_id
        if key_id is None:
            found = find_key(github.list_deploy_keys(repository.name), repository.public_key)
            key_id = int(found["id"]) if found else None
        if key_id is None:
            retval = (True, f"{repository.name}: no deploy key on GitHub to revoke")
        elif github.delete_deploy_key(repository.name, key_id):
            retval = (True, f"{repository.name}: deploy key id {key_id} revoked on GitHub")
        else:
            retval = (True, f"{repository.name}: deploy key id {key_id} was already gone from GitHub")
    except GitHubError as error:
        repository.state = PENDING_REVOKE
        retval = (False, f"{repository.name}: revocation failed, kept as pending-revoke for `creds audit`: {error}")
    return retval


def clone_in_backend(backend, repository: RepositoryRecord) -> str:
    """`backend repo add OWNER/REPO` inside the backend, which clones once the key is registered."""
    result = backend.tool(["backend", "repo", "add", repository.name], parse_json=True, json_flag=False)
    repository.checkout = result.get("checkout") if isinstance(result, dict) else None
    retval = f"{repository.name}: cloned to {repository.checkout}" if repository.checkout else f"{repository.name}: clone requested"
    return retval


def register_and_clone(github: GitHub, backend, record: ProjectRecord, command: str, clone: bool = True) -> int:
    """Every unregistered repository of the record: register on GitHub, then clone. The record is
    saved after each step. Returns how many repositories still lack registration."""
    retval = 0
    for repository in record.repos:
        if repository.state != UNREGISTERED:
            continue
        try:
            print(f"{command}: {register_key(github, record, repository)}", flush=True)
        except GitHubError as error:
            print(f"{command}: {repository.name}: registration failed: {error}", file=sys.stderr)
            retval += 1
            continue
        control_inventory.save(record)
        if clone:
            try:
                print(f"{command}: {clone_in_backend(backend, repository)}", flush=True)
            except RemoteError as error:
                print(f"{command}: {repository.name}: clone failed: {error}; `sandboxed-devenv repo add "
                      f"{record.project} {repository.name}` retries it", file=sys.stderr)
            control_inventory.save(record)
    return retval


def revoke_all(github: GitHub, record: ProjectRecord, command: str) -> list[RepositoryRecord]:
    """Revoke every registered key of a project; returns the entries left pending."""
    retlist = []
    for repository in record.repos:
        if repository.state == UNREGISTERED and repository.github_key_id is None and not repository.public_key:
            continue
        revoked, line = revoke_key(github, repository)
        print(f"{command}: {line}", file=None if revoked else sys.stderr, flush=True)
        if not revoked:
            retlist.append(repository)
    return retlist


def claude_token(user_config: config.UserConfig, project: str, disposable: bool) -> tuple[str | None, str]:
    """The Claude token a backend gets under the policy knob, and a line saying where it came
    from or why there is none (PLAN.md M3)."""
    policy = user_config.backend.claude_token
    if disposable and not user_config.backend.disposable_claude_token:
        retval = (None, "no Claude token: disposable backends get none (backend.disposable_claude_token in config.yaml)")
    elif policy == "none":
        retval = (None, "no Claude token: backend.claude_token is `none` in config.yaml")
    elif policy == "per-project":
        name = f"{config.CLAUDE_TOKEN_NAME}.{project}"
        token = config.read_secret(name)
        if token:
            retval = (token, f"Claude token for this project from {config.secrets_dir() / name}")
        else:
            retval = (None, f"no Claude token: backend.claude_token is `per-project` and {config.secrets_dir() / name} is absent")
    else:
        token = config.read_secret(config.CLAUDE_TOKEN_NAME)
        if token:
            retval = (token, f"shared Claude token from {config.secrets_dir() / config.CLAUDE_TOKEN_NAME}")
        else:
            retval = (None, f"no Claude token: {config.secrets_dir() / config.CLAUDE_TOKEN_NAME} is absent; Claude Code asks to log in")
    return retval


def token_fingerprint(token: str) -> str:
    retval = "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    return retval


def record_token(record: ProjectRecord, token: str | None) -> None:
    """The Claude token's presence in a backend, as a credential with its hashed fingerprint."""
    kept = []
    for credential in record.credentials:
        if credential.kind != CLAUDE_TOKEN_KIND:
            kept.append(credential)
    record.credentials = kept
    if token:
        record.credentials.append(CredentialRecord(
            kind=CLAUDE_TOKEN_KIND, scope=f"Claude Code in {record.vm_name}", fingerprint=token_fingerprint(token),
            location=CLAUDE_SETTINGS_PATH, revoke=CLAUDE_TOKEN_REVOKE, created=control_inventory.now()))


# --- commands ---------------------------------------------------------------------------------------


def register(subparsers):
    repo = common.add_group(subparsers, "repo", "a project's repositories and their deploy keys (control)")

    add = repo.add_parser(
        "add",
        help="mint a key pair in the backend, register it on GitHub, record it, clone to ~/proj/PROJECT/REPO",
    )
    add.add_argument("project", metavar="PROJECT")
    add.add_argument("repository", metavar="OWNER/REPO")
    add.add_argument(
        "--deploy-key-grants-repo-write",
        action="store_true",
        help="register the deploy key with write access (the default key is read-only)",
    )
    add.add_argument("--no-clone", action="store_true", help="register the key but do not clone")
    add.set_defaults(handler=cmd_repo_add)

    rm = repo.add_parser("rm", help="revoke the key on GitHub, then remove it and its alias in the backend")
    rm.add_argument("project", metavar="PROJECT")
    rm.add_argument("repository", metavar="OWNER/REPO")
    rm.add_argument("--delete-checkout", action="store_true", help="also delete ~/proj/PROJECT/REPO")
    rm.set_defaults(handler=cmd_repo_rm)

    ls = repo.add_parser("ls", help="the project's repositories with key fingerprint, access and age")
    ls.add_argument("project", metavar="PROJECT")
    ls.add_argument("--json", action="store_true", help="machine-readable")
    ls.set_defaults(handler=cmd_repo_ls)

    creds = common.add_group(subparsers, "creds", "every credential: list, rotate, audit (control)")

    creds_ls = creds.add_parser("ls", help="every credential, scope, age, where it lives, how it is revoked")
    creds_ls.add_argument("project", metavar="PROJECT", nargs="?", help="one project, or every project")
    creds_ls.add_argument("--json", action="store_true", help="machine-readable")
    creds_ls.set_defaults(handler=cmd_creds_ls)

    rotate = creds.add_parser("rotate", help="regenerate inside the backend, re-register, update the inventory")
    rotate.add_argument("project", metavar="PROJECT")
    rotate.add_argument(
        "--deploy-key",
        metavar="OWNER/REPO",
        nargs="?",
        const=ALL_REPOSITORIES,
        help="rotate one repository's deploy key, or every repository's when no OWNER/REPO is given",
    )
    rotate.add_argument("--claude-token", action="store_true", help="replace the Claude token from the secrets dir")
    rotate.set_defaults(handler=cmd_creds_rotate)

    audit = creds.add_parser(
        "audit",
        help="compare the inventory with GitHub's deploy keys: retry pending revocations, adopt or report unclaimed keys",
    )
    audit.add_argument("--fix", action="store_true", help="delete the unclaimed keys this tool made (by title) that no project claims")
    audit.set_defaults(handler=cmd_creds_audit)


def backend_for(record: ProjectRecord, runner: common.Runner):
    if not record.address:
        raise InventoryError(f"{record.project} has no backend address in the inventory")
    if record.removed:
        raise InventoryError(f"{record.project}'s backend was removed on {record.removed:%Y-%m-%d}; only its pending "
                             "revocations remain (`creds audit`)")
    retval = remote.backend(record.address, record.vm_name, runner)
    return retval


def repo_add(args) -> int:
    command = "repo add"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    record = control_inventory.require(args.project)
    try:
        config.Repository(name=args.repository)
    except pydantic.ValidationError:
        raise InventoryError(f"a repository is named OWNER/REPO, not {args.repository!r}") from None
    access = "write" if args.deploy_key_grants_repo_write else "read"
    existing = None
    for repository in record.repos:
        if repository.name == args.repository:
            existing = repository
            break
    if args.dry_run:
        if existing and existing.state == ACTIVE:
            print(f"would check that {args.repository} is registered (it is, id {existing.github_key_id})"
                  f"{'' if args.no_clone else ' and clone it unless already cloned'}")
        else:
            print(f"would run `backend repo add {args.repository} --no-clone` in {record.vm_name}, register the key on "
                  f"GitHub ({access}){'' if args.no_clone else ', then clone'}, and record it in the inventory")
        retval = 0
        return retval
    github = GitHub(runner)
    require_gh()
    backend = backend_for(record, runner)
    if existing is None:
        minted = backend.tool(["backend", "repo", "add", args.repository, "--no-clone"], parse_json=True, json_flag=False)
        existing = RepositoryRecord(name=args.repository, access=access, fingerprint=minted.get("fingerprint"),
                                    public_key=minted.get("public_key") or None, created=control_inventory.now())
        record.repos.append(existing)
        control_inventory.save(record)
        print(f"{command}: {args.repository}: key pair {'minted' if minted.get('created') else 'present'} in {record.vm_name} "
              f"({existing.fingerprint})", flush=True)
    elif existing.state == ACTIVE:
        print(f"{command}: {args.repository} is already registered (id {existing.github_key_id})", flush=True)
    if existing.state == ACTIVE and not args.no_clone and existing.checkout is None:
        print(f"{command}: {clone_in_backend(backend, existing)}", flush=True)
        control_inventory.save(record)
        retval = 0
        return retval
    left = register_and_clone(github, backend, record, command, clone=not args.no_clone)
    retval = 1 if left else 0
    return retval


def cmd_repo_add(args) -> int:
    try:
        retval = repo_add(args)
    except ERRORS as error:
        retval = remote.report(error, "repo add")
    return retval


def repo_rm(args) -> int:
    command = "repo rm"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    record = control_inventory.require(args.project)
    target = None
    for repository in record.repos:
        if repository.name == args.repository:
            target = repository
            break
    if target is None:
        raise InventoryError(f"{args.repository} is not a repository of {args.project}; `sandboxed-devenv repo ls {args.project}`")
    if args.dry_run:
        print(f"would revoke deploy key {target.github_key_id or '(by text)'} for {args.repository} on GitHub, then run "
              f"`backend repo rm {args.repository}{' --delete-checkout' if args.delete_checkout else ''}` in {record.vm_name}")
        retval = 0
        return retval
    github = GitHub(runner)
    require_gh()
    revoked, line = revoke_key(github, target)
    print(f"{command}: {line}", file=None if revoked else sys.stderr, flush=True)
    if not revoked:
        control_inventory.save(record)
        retval = 1
        return retval
    backend = backend_for(record, runner)
    arguments = ["backend", "repo", "rm", args.repository]
    if args.delete_checkout:
        arguments.append("--delete-checkout")
    result = backend.tool(arguments, parse_json=True, json_flag=False)
    record.repos.remove(target)
    control_inventory.save(record)
    removed = result.get("removed") if isinstance(result, dict) else None
    print(f"{command}: {args.repository}: {len(removed or [])} files removed in {record.vm_name}"
          f"{'' if args.delete_checkout else '; the checkout stays'}")
    retval = 0
    return retval


def cmd_repo_rm(args) -> int:
    try:
        retval = repo_rm(args)
    except ERRORS as error:
        retval = remote.report(error, "repo rm")
    return retval


def age(moment: datetime.datetime | None) -> str:
    if moment is None:
        retval = "?"
    else:
        days = (control_inventory.now() - moment).days
        retval = f"{days} d"
    return retval


def print_table(rows: list[tuple]) -> None:
    widths = [0] * len(rows[0])
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def repo_ls(args) -> int:
    record = control_inventory.require(args.project)
    if args.json:
        print(json.dumps([repository.model_dump(mode="json") for repository in record.repos], indent=2))
    elif not record.repos:
        print(f"{args.project}: no repositories; `sandboxed-devenv repo add {args.project} OWNER/REPO` adds one")
    else:
        rows = [("REPOSITORY", "ACCESS", "STATE", "GITHUB ID", "FINGERPRINT", "AGE", "CHECKOUT")]
        for repository in record.repos:
            rows.append((repository.name, repository.access, repository.state, str(repository.github_key_id or "-"),
                         repository.fingerprint or "?", age(repository.created), repository.checkout or "not cloned"))
        print_table(rows)
    retval = 0
    return retval


def cmd_repo_ls(args) -> int:
    try:
        retval = repo_ls(args)
    except ERRORS as error:
        retval = remote.report(error, "repo ls")
    return retval


def credential_rows(records: list[ProjectRecord]) -> list[dict]:
    """Every credential the inventory knows, plus the two key pairs from the secrets directory."""
    retlist = []
    pairs = [
        ("control-key", remote.control_key_path(), "the control side into desk and every backend",
         f"{remote.control_key_path()} only",
         "replace the pair, then `client-host new --replace` and `rm` plus `new` for every backend"),
        ("sandbox-key", remote.sandbox_key_path(), "gw into every backend",
         f"{remote.sandbox_key_path()}, desk's secrets directory, /home/gw/.ssh/vm_ed25519",
         "replace the pair, then `client-host provision` and `rm` plus `new` for every backend"),
    ]
    for kind, key, scope, location, revoke in pairs:
        if key.is_file():
            created = datetime.datetime.fromtimestamp(key.stat().st_mtime, datetime.UTC).replace(microsecond=0)
            retlist.append({
                "project": "-", "kind": kind, "scope": scope, "state": ACTIVE, "id": None,
                "fingerprint": key_fingerprint(key.with_suffix(".pub")), "location": location,
                "created": created.isoformat(), "revoke": revoke,
            })
    for record in records:
        for repository in record.repos:
            retlist.append({
                "project": record.project, "kind": "deploy-key", "scope": f"{repository.name} ({repository.access})",
                "state": repository.state, "id": repository.github_key_id, "fingerprint": repository.fingerprint,
                "location": f"/home/dev/.ssh/deploy_{repository.name.replace('/', '_')} in {record.vm_name}",
                "created": repository.created.isoformat() if repository.created else None,
                "revoke": f"`sandboxed-devenv repo rm {record.project} {repository.name}` (gh api DELETE repos/{repository.name}/keys/ID)",
            })
        for credential in record.credentials:
            retlist.append({
                "project": record.project, "kind": credential.kind, "scope": credential.scope, "state": credential.state,
                "id": None, "fingerprint": credential.fingerprint, "location": credential.location,
                "created": credential.created.isoformat() if credential.created else None, "revoke": credential.revoke,
            })
    return retlist


def creds_ls(args) -> int:
    if args.project:
        records = [control_inventory.require(args.project)]
    else:
        records = control_inventory.list_projects()
    rows = credential_rows(records)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no credentials: no key pairs yet and no projects in the inventory")
    else:
        table = [("PROJECT", "KIND", "SCOPE", "STATE", "ID", "FINGERPRINT", "AGE", "LOCATION")]
        for row in rows:
            created = datetime.datetime.fromisoformat(row["created"]) if row["created"] else None
            table.append((row["project"], row["kind"], row["scope"], row["state"], str(row["id"] or "-"),
                          row["fingerprint"] or "?", age(created), row["location"]))
        print_table(table)
        print()
        print("revocation:")
        seen = set()
        for row in rows:
            line = f"  {row['kind']}: {row['revoke']}"
            if line not in seen:
                seen.add(line)
                print(line)
    retval = 0
    return retval


def cmd_creds_ls(args) -> int:
    try:
        retval = creds_ls(args)
    except ERRORS as error:
        retval = remote.report(error, "creds ls")
    return retval


def rotate_deploy_keys(github: GitHub, backend, record: ProjectRecord, which: str, command: str) -> int:
    """A fresh key pair per repository (`backend repo rekey`), the old key revoked, the new one
    registered; returns how many problems remain."""
    retval = 0
    for repository in record.repos:
        if which != ALL_REPOSITORIES and repository.name != which:
            continue
        old_id = repository.github_key_id
        rekeyed = backend.tool(["backend", "repo", "rekey", repository.name], parse_json=True, json_flag=False)
        repository.public_key = rekeyed.get("public_key") or repository.public_key
        repository.fingerprint = rekeyed.get("fingerprint") or repository.fingerprint
        repository.created = control_inventory.now()
        print(f"{command}: {repository.name}: new key pair in {record.vm_name} ({repository.fingerprint})", flush=True)
        if old_id is not None:
            revoked, line = revoke_key(github, RepositoryRecord(name=repository.name, github_key_id=old_id))
            print(f"{command}: {line}", file=None if revoked else sys.stderr, flush=True)
            if not revoked:
                retval += 1
        repository.github_key_id = None
        repository.state = UNREGISTERED
        control_inventory.save(record)
        try:
            print(f"{command}: {register_key(github, record, repository)}", flush=True)
        except GitHubError as error:
            print(f"{command}: {repository.name}: registration of the new key failed: {error}", file=sys.stderr)
            retval += 1
        control_inventory.save(record)
    return retval


def rotate_token(backend, record: ProjectRecord, command: str) -> None:
    """The Claude token from the secrets directory into the backend: project.yaml rewritten in
    place through `sudo tee` (the file keeps its root-only mode), then `backend prepare` renders
    dev's settings.json from it."""
    token, source = claude_token(config.load_user_config(), record.project, record.disposable)
    if not token:
        raise InventoryError(f"{source}; `claude setup-token` makes one")
    current = backend.run(["sudo", "cat", str(config.BACKEND_PROJECT_CONFIG)], capture=True).stdout or ""
    project = config.ProjectConfig(**(config.yaml.safe_load(current) or {})) if current.strip() else None
    if project is None:
        raise RemoteError(f"{record.vm_name} has no readable {config.BACKEND_PROJECT_CONFIG}")
    project.claude_oauth_token = token
    backend.run(["sudo", "tee", str(config.BACKEND_PROJECT_CONFIG)], input=project.to_yaml(), quiet=True)
    backend.run(["sudo", "chmod", "600", str(config.BACKEND_PROJECT_CONFIG)])
    backend.tool(["backend", "prepare", "--stage", "project"], sudo=True)
    record_token(record, token)
    control_inventory.save(record)
    print(f"{command}: Claude token in {record.vm_name} replaced ({token_fingerprint(token)}); {source}", flush=True)


def creds_rotate(args) -> int:
    command = "creds rotate"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    record = control_inventory.require(args.project)
    if not args.deploy_key and not args.claude_token:
        print(f"sandboxed-devenv: `{command}` needs --deploy-key [OWNER/REPO] or --claude-token, or both", file=sys.stderr)
        retval = 2
        return retval
    if args.dry_run:
        if args.deploy_key:
            which = "every repository" if args.deploy_key == ALL_REPOSITORIES else args.deploy_key
            print(f"would rekey {which} in {record.vm_name}, revoke the old key on GitHub and register the new one")
        if args.claude_token:
            print(f"would write the token from {config.secrets_dir() / config.CLAUDE_TOKEN_NAME} into {record.vm_name}'s "
                  "project.yaml and run `backend prepare --stage project` there")
        retval = 0
        return retval
    backend = backend_for(record, runner)
    problems = 0
    if args.deploy_key:
        if args.deploy_key != ALL_REPOSITORIES and not any(r.name == args.deploy_key for r in record.repos):
            raise InventoryError(f"{args.deploy_key} is not a repository of {args.project}")
        require_gh()
        problems += rotate_deploy_keys(GitHub(runner), backend, record, args.deploy_key, command)
    if args.claude_token:
        rotate_token(backend, record, command)
    retval = 1 if problems else 0
    return retval


def cmd_creds_rotate(args) -> int:
    try:
        retval = creds_rotate(args)
    except ERRORS as error:
        retval = remote.report(error, "creds rotate")
    return retval


def audit(github: GitHub, records: list[ProjectRecord], fix: bool, command: str = "creds audit") -> int:
    """Compare the inventory with GitHub, repository by repository; returns the number of
    problems left. Pending revocations are retried, keys registered by hand are adopted, missing
    keys are reported, and unclaimed keys are reported, or deleted with --fix when their title
    says this tool made them."""
    problems = 0
    by_repository = {}
    for record in records:
        for repository in record.repos:
            by_repository.setdefault(repository.name, []).append((record, repository))
    changed = set()
    for name in sorted(by_repository):
        try:
            listed = github.list_deploy_keys(name)
        except GitHubError as error:
            print(f"{command}: {name}: cannot list GitHub's keys: {error}", file=sys.stderr)
            problems += 1
            continue
        claimed = set()
        revoked_any = False
        for record, repository in by_repository[name]:
            if repository.state == PENDING_REVOKE:
                revoked, line = revoke_key(github, repository)
                print(f"{command}: {record.project}: {line}", flush=True)
                if revoked:
                    record.repos.remove(repository)
                    changed.add(record.project)
                    revoked_any = True
                else:
                    problems += 1
                continue
            found = find_key(listed, repository.public_key)
            if repository.state == UNREGISTERED:
                if found is not None:
                    repository.github_key_id = int(found["id"])
                    repository.title = found.get("title")
                    repository.state = ACTIVE
                    repository.registered = control_inventory.now()
                    changed.add(record.project)
                    claimed.add(int(found["id"]))
                    print(f"{command}: {record.project}: {name}: adopted key id {found['id']} ({found.get('title')}), registered by hand")
                else:
                    print(f"{command}: {record.project}: {name}: key not registered; `sandboxed-devenv repo add {record.project} {name}`")
                    problems += 1
                continue
            if repository.github_key_id is not None and any(int(item["id"]) == repository.github_key_id for item in listed):
                claimed.add(repository.github_key_id)
            elif found is not None:
                print(f"{command}: {record.project}: {name}: key id changed on GitHub to {found['id']}; recorded")
                repository.github_key_id = int(found["id"])
                claimed.add(int(found["id"]))
                changed.add(record.project)
            else:
                print(f"{command}: {record.project}: {name}: key id {repository.github_key_id} is gone from GitHub; marked unregistered")
                repository.github_key_id = None
                repository.state = UNREGISTERED
                changed.add(record.project)
                problems += 1
        if revoked_any:
            try:
                listed = github.list_deploy_keys(name)
            except GitHubError as error:
                print(f"{command}: {name}: cannot list GitHub's keys again: {error}", file=sys.stderr)
                problems += 1
                continue
        for item in listed:
            if int(item["id"]) in claimed:
                continue
            ours = str(item.get("title", "")).startswith(TITLE_PREFIX)
            if ours and fix:
                try:
                    github.delete_deploy_key(name, int(item["id"]))
                    print(f"{command}: {name}: deleted unclaimed key id {item['id']} ({item.get('title')})")
                except GitHubError as error:
                    print(f"{command}: {name}: could not delete key id {item['id']}: {error}", file=sys.stderr)
                    problems += 1
            else:
                how = "--fix deletes it" if ours else "not this tool's title, left alone"
                print(f"{command}: {name}: unclaimed key id {item['id']} ({item.get('title')}, {item.get('created_at', '?')[:10]}); {how}")
                problems += 1
    for record in records:
        if record.project in changed:
            if record.removed and not record.repos:
                control_inventory.delete(record.project)
                print(f"{command}: {record.project}: every revocation done; the record of the removed backend is gone")
            else:
                control_inventory.save(record)
    retval = problems
    return retval


def creds_audit(args) -> int:
    command = "creds audit"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    records = control_inventory.list_projects()
    if not records:
        print(f"{command}: no projects in the inventory; nothing to compare")
        retval = 0
        return retval
    require_gh()
    problems = audit(GitHub(runner), records, args.fix and not args.dry_run, command)
    print(f"{command}: {problems} problem(s)")
    retval = 1 if problems else 0
    return retval


def cmd_creds_audit(args) -> int:
    try:
        retval = creds_audit(args)
    except ERRORS as error:
        retval = remote.report(error, "creds audit")
    return retval
