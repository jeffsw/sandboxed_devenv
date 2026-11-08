"""One backend per project: new, ls, start, stop, rm, clone, resources, ssh, serial-console (control role).

Every command takes the project's name and finds the backend through the inventory; no command
takes a VM name (PLAN.md, "Projects and repositories"). `new` launches the backend from the base
image: the driver imports the image (an APFS clone under UTM, instant and block-sharing) with a
seed rendered from the project cloud-init (hostname, the rig key, project.yaml with the
repositories, git identity and Claude token, a static address from the pool), waits for SSH and
for `backend status` to report ready, pins the host keys on the client host with `connect add`,
registers each repository's minted deploy key on GitHub and clones it (control_creds.py), and
records everything in the inventory. When GitHub cannot be reached the keys stay unregistered and
the commands to finish by hand are printed.

`clone` stops the source backend for a consistent copy of its disk, launches the copy as a
disposable project with its own seed (a new instance id, so cloud-init treats the first boot as
new: hostname, address, host keys and project.yaml all change) and gives every repository a
fresh key pair, registered under the new project's name. `rm` revokes every deploy key on GitHub
first, removes the connection on the client host and deletes the VM; a revocation that fails
keeps the inventory record, marked removed, for `creds audit` to retry. `resources` reads the
VM's own configuration and changes it with the backend
stopped where the driver needs that. `ssh` and `serial-console` exec into the VM.

Slow steps say what they do and why before they start, and how long they took (PLAN.md,
"Principles").
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time

from sandboxed_devenv import (
    common,
    config,
    control_creds,
    control_driver,
    control_images,
    control_inventory,
    host_client,
    host_connect,
    remote,
    render,
)
from sandboxed_devenv.control_driver import DriverError, VmSpec
from sandboxed_devenv.control_inventory import UNREGISTERED, InventoryError, ProjectRecord, RepositoryRecord
from sandboxed_devenv.remote import RemoteError
from sandboxed_devenv.render import RenderError

# First login: the users module runs early in boot.
SSH_TIMEOUT = 600
# `backend prepare` at first boot: GitHub's host keys, a key pair per repository; a minute.
READY_TIMEOUT = 900
STOP_TIMEOUT = 180
FORCE_STOP_TIMEOUT = 60
NEW_DIR_PREFIX = ".new-"
ERRORS = (RemoteError, DriverError, RenderError, InventoryError, config.SecretError, OSError)


def _project_argument(parser: argparse.ArgumentParser, metavar: str = "PROJECT") -> None:
    parser.add_argument("project", metavar=metavar, help="the project; its backend is found through the inventory")


def _resource_arguments(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--cpus", metavar="N", type=int, help=f"CPUs for the backend: {what}")
    parser.add_argument("--memory-gib", metavar="N", type=int, help=f"memory for the backend, in GiB: {what}")


def _yes_argument(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--yes", action="store_true", help=f"{what} without asking, for scripts")


def register(subparsers):
    new = subparsers.add_parser(
        "new",
        help="launch the backend for PROJECT from the base image and register it (control)",
    )
    _project_argument(new)
    new.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        action="append",
        default=[],
        help="a repository to add; repeatable, and repositories can be added later with `repo add`",
    )
    new.add_argument(
        "--disposable",
        action="store_true",
        help="a backend for stranger code, used once and destroyed: `ls` flags it and `rm` takes no snapshot",
    )
    new.add_argument(
        "--deploy-key-grants-repo-write",
        action="store_true",
        help="register each --repo's deploy key with write access (the default key is read-only)",
    )
    _resource_arguments(new, "override the default in config.yaml")
    new.set_defaults(handler=cmd_new)

    ls = subparsers.add_parser("ls", help="projects with their backend's state, address, resources, repositories (control)")
    ls.add_argument("--json", action="store_true", help="machine-readable")
    ls.set_defaults(handler=cmd_ls)

    start = subparsers.add_parser("start", help="start the project's backend (control)")
    _project_argument(start)
    start.set_defaults(handler=cmd_start)

    stop = subparsers.add_parser("stop", help="stop the project's backend: a shutdown request, then a wait (control)")
    _project_argument(stop)
    stop.add_argument("--force", action="store_true", help="pull the plug instead of asking the guest to shut down")
    stop.set_defaults(handler=cmd_stop)

    rm = subparsers.add_parser(
        "rm",
        help="revoke the backend's deploy keys on GitHub, remove the connection on the client host, delete the VM (control)",
    )
    _project_argument(rm)
    _yes_argument(rm, "delete")
    rm.set_defaults(handler=cmd_rm)

    clone = subparsers.add_parser(
        "clone",
        help="a disposable backend for NEW_PROJECT copied from PROJECT's, with fresh keys (control)",
    )
    _project_argument(clone)
    clone.add_argument("new_project", metavar="NEW_PROJECT", help="the new, short-lived project")
    _yes_argument(clone, "stop PROJECT's backend for the copy")
    clone.set_defaults(handler=cmd_clone)

    resources = subparsers.add_parser(
        "resources",
        help="show or change the backend's CPUs and memory; asks before a stop and start when one is needed (control)",
    )
    _project_argument(resources)
    _resource_arguments(resources, "the new value; without either flag the current values are shown")
    _yes_argument(resources, "approve the stop and start")
    resources.set_defaults(handler=cmd_resources)

    ssh = subparsers.add_parser("ssh", help="a shell as `dev` in the project's backend with the rig key (control)")
    _project_argument(ssh)
    ssh.add_argument("cmd", metavar="CMD", nargs=argparse.REMAINDER,
                     help="a command to run there instead of a shell. No shell expansion.")
    ssh.set_defaults(handler=cmd_ssh)

    serial_console = subparsers.add_parser(
        "serial-console",
        help="the backend's serial console through the driver, for when SSH is not up (control)",
    )
    _project_argument(serial_console)
    serial_console.set_defaults(handler=cmd_serial_console)


# --- shared pieces -----------------------------------------------------------------------------


def setup(args) -> tuple[common.Runner, config.UserConfig, control_driver.Driver]:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    driver = control_driver.load_driver(runner)
    retval = (runner, user_config, driver)
    return retval


def hostname_for(vm_name: str) -> str:
    """A VM name as a hostname: letters, digits and hyphens only (`idesbx-ide_sandbox` has an
    underscore, which a hostname may not)."""
    cleaned = []
    for character in vm_name:
        cleaned.append(character if character.isalnum() or character == "-" else "-")
    retval = "".join(cleaned).strip("-")[:63] or "backend"
    return retval


def git_identity(user_config: config.UserConfig, runner: common.Runner) -> config.GitIdentity:
    """From config.yaml, else this machine's global git config."""
    retval = user_config.git
    if retval is None:
        values = {}
        for key in ("user.name", "user.email"):
            completed = runner.run(["git", "config", "--global", key], capture_output=True)
            values[key] = (completed.stdout or "").strip() if completed.returncode == 0 else ""
        if runner.dry_run:
            values = {"user.name": values["user.name"] or "Developer", "user.email": values["user.email"] or "dev@example.invalid"}
        if not (values["user.name"] and values["user.email"]):
            raise RemoteError("no git identity for commits inside the backend: set `git: {user_name, user_email}` in "
                              f"{config.config_file()} or `git config --global user.name` and `user.email` here")
        retval = config.GitIdentity(user_name=values["user.name"], user_email=values["user.email"])
    return retval


def base_image(driver: control_driver.Driver) -> control_images.ImageRecord:
    """The base image for the running package version, which the driver must hold."""
    name = control_images.image_name(common.package_version())
    retval = control_images.load_record(name)
    if retval is None or not driver.image_exists(name):
        raise RemoteError(f"no base image {driver.image_location(name)} for package {common.package_version()}; "
                          "`sandboxed-devenv image build` makes it")
    retval.allocated_bytes, retval.size_bytes = driver.image_size(name)
    return retval


def project_config(project: str, repositories: list[str], write: bool, identity: config.GitIdentity,
                   token: str | None) -> config.ProjectConfig:
    repos = []
    for name in repositories:
        repos.append(config.Repository(name=name, access="write" if write else "read"))
    retval = config.ProjectConfig(project=project, repositories=repos, git=identity, claude_oauth_token=token,
                                  created=control_inventory.now())
    return retval


def write_project_seed(seed_dir, user_config: config.UserConfig, project: config.ProjectConfig, hostname: str,
                       address: str, keys: dict[str, str], host_key: tuple[str, str], static_network: bool = True) -> None:
    """The NoCloud seed for a project's backend: the project user-data (the control and sandbox
    keys for dev, the VM's own host key pair), meta-data with a fresh instance id, and the static
    network-config where the driver's network has no leases of its own."""
    values = render.default_values(user_config, **keys)
    values.update(render.host_key_values(*host_key))
    values.update(render.project_values(project, hostname, address))
    user_data = render.cloudinit_document("backend", "cloudinit-project", "user-data", values)
    network_config = render.cloudinit_document("backend", "cloudinit", "network-config", values) if static_network else None
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    render.write_seed(seed_dir, user_data, f"{hostname}-{stamp}", hostname, network_config)


def new_record(user_config: config.UserConfig, driver_name: str, image: control_images.ImageRecord, project: str,
               address: str, cpus: int, memory_gib: int, disposable: bool, cloned_from: str | None = None) -> ProjectRecord:
    retval = ProjectRecord(
        project=project, vm_name=user_config.vm_name(project), driver=driver_name, image=image.name,
        package_version=image.package_version, address=address, cpus=cpus, memory_gib=memory_gib,
        disk_gib=image.disk_gib, disposable=disposable, cloned_from=cloned_from, created=control_inventory.now(),
    )
    return retval


def repository_records(status: dict, wanted: list[config.Repository]) -> list[RepositoryRecord]:
    """The inventory's view of the repositories, from `backend status`: fingerprints and public
    keys of the key pairs the backend minted."""
    reported = {}
    for item in status.get("repositories") or []:
        reported[item.get("repository")] = item
    retlist = []
    for repository in wanted:
        item = reported.get(repository.name, {})
        retlist.append(RepositoryRecord(
            name=repository.name, access=repository.access, checkout=item.get("checkout"),
            fingerprint=item.get("fingerprint"), public_key=item.get("public_key") or None,
            created=control_inventory.now(),
        ))
    return retlist


def wait_until_ready(backend: remote.Remote, command: str) -> dict:
    with common.slow_step(command, "first boot", f"waiting for SSH as {backend.target}; the users module runs early in boot"):
        backend.wait_for_ssh(SSH_TIMEOUT)
    with common.slow_step(command, "prepare", "waiting for cloud-init and `backend prepare`: GitHub's host keys, "
                          "the git identity, a key pair per repository"):
        retval = backend.wait_ready(READY_TIMEOUT)
    return retval


def client_connect(user_config: config.UserConfig, runner: common.Runner, arguments: list[str], command: str) -> bool:
    """`connect add` or `connect rm` on the client host, as root there; a failure is reported and
    returns False rather than failing the whole command, since the files can be fixed by hand."""
    client = remote.client_host(user_config, runner)
    try:
        client.tool(arguments, sudo=True)
        retval = True
    except RemoteError as error:
        print(common.warning(f"{command}: on the client host ({client.target}): {error}"), file=sys.stderr)
        print(common.warning(f"{command}: run there by hand: sudo sandboxed-devenv {' '.join(arguments[:3])} ..."), file=sys.stderr)
        retval = False
    return retval


def print_repositories(record: ProjectRecord) -> None:
    """For deploy keys that could not be registered: the commands that register and clone each
    one by hand, or `repo add`, which retries the whole step."""
    unregistered = []
    for repository in record.repos:
        if repository.state == UNREGISTERED:
            unregistered.append(repository)
    if not unregistered:
        return
    print(common.warning(f"{record.project}: {len(unregistered)} deploy key(s) not registered; `sandboxed-devenv repo add "
                         f"{record.project} OWNER/REPO` retries each, or by hand:"))
    for repository in unregistered:
        owner, repo = repository.name.split("/", 1)
        key_file = f"/home/dev/.ssh/deploy_{owner}_{repo}.pub"
        local_copy = f"{record.vm_name}-{owner}-{repo}.pub"
        write = " --allow-write" if repository.access == "write" else ""
        print(f"  {repository.name} ({repository.access}): {repository.public_key or 'key not reported'}")
        print(f"    sandboxed-devenv ssh {record.project} -- cat {key_file} > {local_copy}")
        print(f"    gh repo deploy-key add {local_copy} --repo {repository.name} --title {record.vm_name}{write}")
        print(f"    sandboxed-devenv ssh {record.project} -- sandboxed-devenv backend repo add {repository.name}")
    print(f"  then open /home/dev/proj/{record.project} in Gateway")


def confirm(question: str, assume_yes: bool) -> bool:
    """A yes from the terminal, or from --yes; without a terminal the answer is no."""
    if assume_yes:
        retval = True
    elif not sys.stdin.isatty():
        print(f"{question} refusing without --yes, since there is no terminal to ask", file=sys.stderr)
        retval = False
    else:
        answer = input(f"{question} [y/N] ").strip().lower()
        retval = answer in ("y", "yes")
    return retval


def stop_vm(driver: control_driver.Driver, vm: str, command: str, force: bool = False) -> None:
    """A shutdown request and a wait; the plug pulled when asked or when the guest does not stop."""
    if force:
        driver.stop(vm, force=True)
        control_driver.wait_for_state(driver, vm, control_driver.STOPPED, FORCE_STOP_TIMEOUT)
        return
    driver.stop(vm, force=False)
    with common.slow_step(command, "stop", f"asked {vm} to shut down; waiting for it to stop"):
        try:
            control_driver.wait_for_state(driver, vm, control_driver.STOPPED, STOP_TIMEOUT)
        except DriverError:
            print(common.warning(f"{command}: {vm} did not stop within {STOP_TIMEOUT} s; forcing it"), flush=True)
            driver.stop(vm, force=True)
            control_driver.wait_for_state(driver, vm, control_driver.STOPPED, FORCE_STOP_TIMEOUT)


def vm_state(driver: control_driver.Driver, vm: str) -> str:
    """The driver's state, or `missing` when the VM is gone."""
    if driver.exists(vm):
        retval = driver.state(vm)
    else:
        retval = "missing"
    return retval


def launch(user_config: config.UserConfig, driver: control_driver.Driver, runner: common.Runner, record: ProjectRecord,
           project: config.ProjectConfig, command: str, why_disk: str, image: str | None = None,
           source: str | None = None) -> dict:
    """Create the VM from the base image, or as a copy of a stopped VM, with the project's seed;
    start it, wait for ready, pin the host keys on the client host; returns the backend's status
    report. The inventory record is saved before the VM starts, so a failure leaves something
    `rm` can clean up."""
    hostname = hostname_for(record.vm_name)
    keys = control_creds.seed_key_values(False, runner)
    host_private, host_public = remote.generate_host_key(hostname)
    record.ssh_host_key = host_public
    work_dir = config.images_dir() / f"{NEW_DIR_PREFIX}{record.project}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    try:
        seed_dir = work_dir / "seed"
        write_project_seed(seed_dir, user_config, project, hostname, record.address, keys, (host_private, host_public),
                           driver.static_network)
        del host_private
        remote.forget_host_keys(record.vm_name)
        remote.pin_host_key(record.vm_name, record.address, host_public)
        spec = VmSpec(name=record.vm_name, disk=None, cpus=record.cpus, memory_gib=record.memory_gib, seed_dir=seed_dir,
                      image=image, address=None if driver.static_network else record.address, disk_gib=record.disk_gib)
        with common.slow_step(command, "create", why_disk):
            if source is not None:
                driver.clone_from(source, spec)
            else:
                driver.create(spec)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    control_inventory.save(record)
    driver.start(record.vm_name)
    backend = remote.backend(record.address, record.vm_name, runner)
    retval = wait_until_ready(backend, command)
    record.repos = repository_records(retval, project.repositories)
    control_inventory.save(record)
    reported = retval.get("ssh_host_keys") or []
    if reported and host_public.split()[:2] not in [key.split()[:2] for key in reported]:
        print(common.warning(f"{command}: the backend reports host keys other than the one in its seed: {reported}; "
                             "the seed's key is what gets pinned"), file=sys.stderr)
    client_connect(user_config, runner, host_connect.add_arguments(record.project, record.address, [host_public]), command)
    return retval


def report(error: Exception, command: str) -> int:
    retval = remote.report(error, command)
    return retval


# --- new -----------------------------------------------------------------------------------------


def new_plan_lines(record: ProjectRecord, project: config.ProjectConfig, image: control_images.ImageRecord,
                   user_config: config.UserConfig) -> list[str]:
    repos = ", ".join(repository.name for repository in project.repositories) or "none"
    retlist = [
        f"would launch {record.vm_name} for project {record.project} from {image.name} "
        f"({control_images.format_size(image.allocated_bytes)} allocated of {control_images.format_size(image.size_bytes)}) "
        f"with {record.cpus} CPUs and {record.memory_gib} GiB at {record.address}",
        f"would render the project seed: hostname {hostname_for(record.vm_name)}, repositories {repos}, "
        f"git identity {project.git.user_name} <{project.git.user_email}>, "
        f"Claude token {'present' if project.claude_oauth_token else 'absent'}{', disposable' if record.disposable else ''}",
        "would mint the VM's SSH host key into the seed and pin it, with the control and sandbox keys for dev",
        f"would start it, wait for SSH as dev@{record.address}, then for `backend status` to report ready",
        f"would run `connect add {record.project} {record.address} HOSTKEY` on the client host ({user_config.client_host})",
        f"would record it in {control_inventory.record_path(record.project)}",
    ]
    if project.repositories:
        retlist.append("would register each repository's minted deploy key on GitHub (gh api) and clone it in the backend")
    return retlist


def new(args) -> int:
    command = "new"
    runner, user_config, driver = setup(args)
    if not control_inventory.valid_project_name(args.project):
        raise InventoryError(f"project names are letters, digits, `_`, `.` and `-`: {args.project!r}")
    if control_inventory.load(args.project) is not None:
        raise InventoryError(f"project {args.project} is already in the inventory; `sandboxed-devenv rm {args.project}` first")
    vm = user_config.vm_name(args.project)
    if driver.exists(vm):
        raise DriverError(f"a VM named {vm} exists but is not in the inventory; delete it or choose another project name")
    image = base_image(driver)
    if image.driver != driver.name:
        raise DriverError(f"{image.name} was built for the {image.driver} driver, not {driver.name}")
    control_creds.seed_key_values(args.dry_run, runner)
    identity = git_identity(user_config, runner)
    token, token_source = control_creds.claude_token(user_config, args.project, args.disposable)
    project = project_config(args.project, args.repo, args.deploy_key_grants_repo_write, identity, token)
    address = control_inventory.allocate_address(driver.address_pool(user_config))
    cpus = args.cpus or user_config.backend.cpus
    memory_gib = args.memory_gib or user_config.backend.memory_gib
    record = new_record(user_config, driver.name, image, args.project, address, cpus, memory_gib, args.disposable)
    if args.dry_run:
        for line in new_plan_lines(record, project, image, user_config):
            print(line)
        retval = 0
        return retval
    github = github_for(args.repo, runner)
    started = time.monotonic()
    print(f"{command}: launching {vm} for {args.project} at {address} with {cpus} CPUs and {memory_gib} GiB "
          f"from {image.name}; {token_source}", flush=True)
    why = (f"the {driver.name} driver makes the VM from {image.name} "
           f"({control_images.format_size(image.allocated_bytes)} allocated of {control_images.format_size(image.size_bytes)}) "
           "and the seed" + ("; APFS shares the blocks, so this should take seconds" if driver.name == "utm" else ""))
    try:
        launch(user_config, driver, runner, record, project, command, why, image=image.name)
    except (RemoteError, DriverError) as error:
        print(f"{command}: failed: {error}", file=sys.stderr)
        print(f"{command}: {vm} is left as it is for inspection (`sandboxed-devenv serial-console {args.project}`, "
              f"`sandboxed-devenv ssh {args.project}`); `sandboxed-devenv rm {args.project}` removes it", file=sys.stderr)
        retval = 1
        return retval
    control_creds.record_token(record, project.claude_oauth_token)
    control_inventory.save(record)
    left = 0
    if record.repos:
        backend = remote.backend(record.address, record.vm_name, runner)
        left = control_creds.register_and_clone(github, backend, record, command)
    print(f"{command}: {args.project} ready: {vm} at {address}, {len(record.repos)} repositories; "
          f"total {common.format_duration(time.monotonic() - started)}")
    print(f"{command}: in Gateway on the client host, connect to dev@{address} with /home/gw/.ssh/vm_ed25519 and open "
          f"/home/dev/proj/{args.project}")
    if left:
        print_repositories(record)
    retval = 1 if left else 0
    return retval


def github_for(repositories: list, runner: common.Runner) -> control_creds.GitHub:
    """The GitHub client, checked before a VM is made when repositories are to be registered, so
    a missing or logged-out `gh` fails early instead of leaving keys unregistered."""
    retval = control_creds.GitHub(runner)
    if repositories:
        control_creds.require_gh()
        if not retval.authenticated():
            raise control_creds.GitHubError("`gh` is not logged in to GitHub; run `gh auth login` first")
    return retval


def cmd_new(args) -> int:
    try:
        retval = new(args)
    except ERRORS as error:
        retval = report(error, "new")
    return retval


# --- ls, start, stop, rm -----------------------------------------------------------------------------


def ls(args) -> int:
    runner, user_config, driver = setup(args)
    records = control_inventory.list_projects()
    states = {}
    for info in driver.list():
        states[info.name] = info.state
    rows = []
    for record in records:
        rows.append({
            "project": record.project,
            "state": "removed" if record.removed else states.get(record.vm_name, "missing"),
            "address": record.address,
            "cpus": record.cpus,
            "memory_gib": record.memory_gib,
            "repos": [repository.name for repository in record.repos],
            "package_version": record.package_version,
            "image": record.image,
            "created": record.created.isoformat() if record.created else None,
            "disposable": record.disposable,
            "cloned_from": record.cloned_from,
            "vm_name": record.vm_name,
            "pending_revocations": len([r for r in record.repos if r.state == control_inventory.PENDING_REVOKE]),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"no projects in {config.inventory_dir()}; `sandboxed-devenv new PROJECT` makes one")
    else:
        table = [("PROJECT", "STATE", "ADDRESS", "CPUS", "MEMORY", "REPOS", "PACKAGE", "CREATED", "NOTES")]
        for row in rows:
            notes = []
            if row["disposable"]:
                notes.append("disposable" + (f" from {row['cloned_from']}" if row["cloned_from"] else ""))
            if row["pending_revocations"]:
                notes.append(f"{row['pending_revocations']} pending revocation(s); `creds audit` retries")
            created = row["created"][:16].replace("T", " ") if row["created"] else "?"
            table.append((row["project"], row["state"], row["address"] or "?", str(row["cpus"]), f"{row['memory_gib']} GiB",
                          str(len(row["repos"])), row["package_version"], created, ", ".join(notes)))
        widths = [0] * len(table[0])
        for line in table:
            for index, cell in enumerate(line):
                widths[index] = max(widths[index], len(cell))
        for line in table:
            print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)).rstrip())
    retval = 0
    return retval


def cmd_ls(args) -> int:
    try:
        retval = ls(args)
    except ERRORS as error:
        retval = report(error, "ls")
    return retval


def start(args) -> int:
    runner, user_config, driver = setup(args)
    record = control_inventory.require(args.project)
    state = vm_state(driver, record.vm_name)
    if state == control_driver.STARTED:
        print(f"start: {record.vm_name} is already started at {record.address}")
    elif state == "missing":
        raise DriverError(f"{record.vm_name} is in the inventory but not in the driver; `sandboxed-devenv rm {args.project}` clears it")
    else:
        driver.start(record.vm_name)
        print(f"start: {record.vm_name} starting at {record.address}; `sandboxed-devenv ssh {args.project}` once it is up")
    retval = 0
    return retval


def cmd_start(args) -> int:
    try:
        retval = start(args)
    except ERRORS as error:
        retval = report(error, "start")
    return retval


def stop(args) -> int:
    runner, user_config, driver = setup(args)
    record = control_inventory.require(args.project)
    state = vm_state(driver, record.vm_name)
    if state == control_driver.STOPPED:
        print(f"stop: {record.vm_name} is already stopped")
    elif state == "missing":
        raise DriverError(f"{record.vm_name} is in the inventory but not in the driver")
    elif args.dry_run:
        driver.stop(record.vm_name, force=args.force)
    else:
        stop_vm(driver, record.vm_name, "stop", force=args.force)
        print(f"stop: {record.vm_name} stopped")
    retval = 0
    return retval


def cmd_stop(args) -> int:
    try:
        retval = stop(args)
    except ERRORS as error:
        retval = report(error, "stop")
    return retval


def rm(args) -> int:
    command = "rm"
    runner, user_config, driver = setup(args)
    record = control_inventory.require(args.project)
    state = vm_state(driver, record.vm_name)
    keyed = [r for r in record.repos if r.github_key_id is not None or r.public_key]
    if args.dry_run:
        if keyed:
            print(f"would revoke the deploy keys of {', '.join(r.name for r in keyed)} on GitHub")
        print(f"would run `connect rm {record.project}` on the client host ({user_config.client_host})")
        if state != "missing":
            print(f"would stop {record.vm_name} ({state}) if needed and delete it")
        print(f"would remove {control_inventory.record_path(record.project)}")
        retval = 0
        return retval
    if not confirm(f"{command}: delete {record.vm_name} ({state}) for project {record.project}, its disk and everything in it?",
                   args.yes):
        retval = 1
        return retval
    pending = []
    if keyed and not record.removed:
        try:
            control_creds.require_gh()
            pending = control_creds.revoke_all(control_creds.GitHub(runner), record, command)
        except control_creds.GitHubError as error:
            print(f"{command}: {error}; the deploy keys stay pending-revoke for `creds audit`", file=sys.stderr)
            for repository in keyed:
                repository.state = control_inventory.PENDING_REVOKE
            pending = keyed
    elif record.removed:
        pending = [r for r in record.repos if r.state == control_inventory.PENDING_REVOKE]
    client_connect(user_config, runner, host_connect.rm_arguments(record.project), command)
    if state != "missing":
        if state != control_driver.STOPPED:
            stop_vm(driver, record.vm_name, command)
        driver.delete(record.vm_name)
    remote.forget_host_keys(record.vm_name)
    if pending:
        record.removed = record.removed or control_inventory.now()
        control_inventory.save(record)
        print(common.warning(f"{command}: {record.project}: {record.vm_name} deleted and the connection removed, but "
                             f"{len(pending)} deploy key revocation(s) failed; the inventory record stays until "
                             "`sandboxed-devenv creds audit` gets them through"))
        retval = 1
    else:
        control_inventory.delete(record.project)
        print(f"{command}: {record.project} removed ({record.vm_name} deleted, deploy keys revoked, connection removed, "
              "inventory record gone)")
        retval = 0
    return retval


def cmd_rm(args) -> int:
    try:
        retval = rm(args)
    except ERRORS as error:
        retval = report(error, "rm")
    return retval


# --- clone -----------------------------------------------------------------------------------------


def clone(args) -> int:
    command = "clone"
    runner, user_config, driver = setup(args)
    source = control_inventory.require(args.project)
    if not control_inventory.valid_project_name(args.new_project):
        raise InventoryError(f"project names are letters, digits, `_`, `.` and `-`: {args.new_project!r}")
    if control_inventory.load(args.new_project) is not None:
        raise InventoryError(f"project {args.new_project} is already in the inventory")
    new_vm = user_config.vm_name(args.new_project)
    if driver.exists(new_vm):
        raise DriverError(f"a VM named {new_vm} exists but is not in the inventory")
    image = control_images.load_record(source.image)
    if image is None:
        raise InventoryError(f"the base image {source.image} that {args.project} came from has no record any more")
    identity = git_identity(user_config, runner)
    token, token_source = control_creds.claude_token(user_config, args.new_project, disposable=True)
    project = config.ProjectConfig(
        project=args.new_project, repositories=[config.Repository(name=r.name, access=r.access) for r in source.repos],
        git=identity, claude_oauth_token=token, created=control_inventory.now())
    address = control_inventory.allocate_address(driver.address_pool(user_config))
    record = new_record(user_config, driver.name, image, args.new_project, address, source.cpus, source.memory_gib,
                        disposable=True, cloned_from=args.project)
    state = vm_state(driver, source.vm_name)
    if args.dry_run:
        print(f"would stop {source.vm_name} ({state}) for a consistent copy, if it is running, and start it again after")
        print(f"would copy {source.vm_name}'s disk out of its bundle (an APFS clone) and launch {new_vm} from it")
        for line in new_plan_lines(record, project, image, user_config)[1:]:
            print(line)
        print("would give every repository in the copy a fresh key pair (`backend repo rekey`), registered under "
              f"{args.new_project}'s name")
        retval = 0
        return retval
    github = github_for(project.repositories, runner)
    was_running = state not in (control_driver.STOPPED, "missing")
    if state == "missing":
        raise DriverError(f"{source.vm_name} is in the inventory but not in the driver")
    if was_running and not confirm(f"{command}: {source.vm_name} is {state}; stop it for a consistent copy of its disk "
                                   "(it is started again afterwards)?", args.yes):
        retval = 1
        return retval
    started = time.monotonic()
    print(f"{command}: {token_source}", flush=True)
    if was_running:
        stop_vm(driver, source.vm_name, command)
    why = (f"the {driver.name} driver copies {source.vm_name}'s disk and takes the new seed"
           + ("; APFS shares the blocks" if driver.name == "utm" else "; a full copy on a dir pool"))
    try:
        status = launch(user_config, driver, runner, record, project, command, why, source=source.vm_name)
    except (RemoteError, DriverError) as error:
        print(f"{command}: failed: {error}", file=sys.stderr)
        print(f"{command}: {new_vm} is left for inspection; `sandboxed-devenv rm {args.new_project}` removes it",
              file=sys.stderr)
        if was_running:
            driver.start(source.vm_name)
        retval = 1
        return retval
    if was_running:
        driver.start(source.vm_name)
        print(f"{command}: {source.vm_name} started again", flush=True)
    backend = remote.backend(address, new_vm, runner)
    rekeyed = []
    for repository in project.repositories:
        result = backend.tool(["backend", "repo", "rekey", repository.name], parse_json=True, json_flag=False)
        rekeyed.append(result)
    control_creds.record_token(record, project.claude_oauth_token)
    left = 0
    if rekeyed:
        status = backend.status()
        record.repos = repository_records(status, project.repositories)
        control_inventory.save(record)
        left = control_creds.register_and_clone(github, backend, record, command)
    control_inventory.save(record)
    print(f"{command}: {args.new_project} ready: {new_vm} at {address}, a disposable copy of {args.project} with "
          f"{len(rekeyed)} fresh deploy keys; total {common.format_duration(time.monotonic() - started)}")
    if left:
        print_repositories(record)
    retval = 1 if left else 0
    return retval


def cmd_clone(args) -> int:
    try:
        retval = clone(args)
    except ERRORS as error:
        retval = report(error, "clone")
    return retval


# --- resources ---------------------------------------------------------------------------------------


def resources(args) -> int:
    command = "resources"
    runner, user_config, driver = setup(args)
    record = control_inventory.require(args.project)
    state = vm_state(driver, record.vm_name)
    live = driver.resources(record.vm_name) if state != "missing" else None
    if args.cpus is None and args.memory_gib is None:
        if live:
            print(f"{record.project}: {record.vm_name} ({state}): {live[0]} CPUs, {live[1]} GiB; "
                  f"inventory says {record.cpus} CPUs, {record.memory_gib} GiB")
        else:
            print(f"{record.project}: {record.vm_name} ({state}): inventory says {record.cpus} CPUs, {record.memory_gib} GiB")
        retval = 0
        return retval
    if state == "missing":
        raise DriverError(f"{record.vm_name} is in the inventory but not in the driver")
    cpus = args.cpus or record.cpus
    memory_gib = args.memory_gib or record.memory_gib
    running = state != control_driver.STOPPED and not driver.live_resources
    if args.dry_run:
        if running:
            print(f"would stop {record.vm_name} ({state}), since the {driver.name} driver changes resources on a stopped VM")
        print(f"would set {record.vm_name} to {cpus} CPUs and {memory_gib} GiB")
        if running:
            print(f"would start {record.vm_name} again")
        retval = 0
        return retval
    if running and not confirm(f"{command}: the {driver.name} driver changes resources on a stopped VM; stop "
                               f"{record.vm_name} ({state}), change it and start it again?", args.yes):
        retval = 1
        return retval
    if running:
        stop_vm(driver, record.vm_name, command)
    driver.set_resources(record.vm_name, cpus=args.cpus, memory_gib=args.memory_gib)
    record.cpus = cpus
    record.memory_gib = memory_gib
    control_inventory.save(record)
    if running:
        driver.start(record.vm_name)
    print(f"{command}: {record.vm_name} now has {cpus} CPUs and {memory_gib} GiB"
          + (" and is starting again" if running else ""))
    retval = 0
    return retval


def cmd_resources(args) -> int:
    try:
        retval = resources(args)
    except ERRORS as error:
        retval = report(error, "resources")
    return retval


# --- ssh, serial-console -----------------------------------------------------------------------------


def ssh(args) -> int:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    record = control_inventory.require(args.project)
    backend = remote.backend(record.address, record.vm_name, runner)
    remote_command = host_client.without_separator(args.cmd)
    command = backend.ssh_command(remote_command or None, tty=not remote_command)
    if args.dry_run:
        print(f"would run: {' '.join(command)}")
        retval = 0
        return retval
    os.execvp(command[0], command)


def cmd_ssh(args) -> int:
    try:
        retval = ssh(args)
    except ERRORS as error:
        retval = report(error, "ssh")
    return retval


def serial_console(args) -> int:
    runner, user_config, driver = setup(args)
    record = control_inventory.require(args.project)
    if not driver.exists(record.vm_name):
        raise DriverError(f"{record.vm_name} is in the inventory but not in the driver")
    command = driver.console_command(record.vm_name)
    if args.dry_run:
        print(f"would run: {' '.join(command)}")
        retval = 0
        return retval
    if command[0] == "screen":
        print(f"serial-console: {record.vm_name} on {command[-1]}; quit screen with ctrl-a k", flush=True)
    os.execvp(command[0], command)


def cmd_serial_console(args) -> int:
    try:
        retval = serial_console(args)
    except ERRORS as error:
        retval = report(error, "serial-console")
    return retval
