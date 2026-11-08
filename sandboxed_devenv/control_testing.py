"""`sandboxed-devenv test ...`: the harnesses that run against real machines (control role, M4).

Never in GitHub CI: these need the hypervisor, the client host and the network. Three sizes:

- `test end-to-end`: all three roles. The base image is built or found current, a throwaway
  project `ci-<stamp>` is launched (disposable, so it carries no token), and then: the backend
  reports ready with the IDE backend registered, the canary passes its golden result in the
  backend role, `host install --check` finds no drift on the client host, the canary passes
  its golden result in the client role there (as gw, which needs a GNOME session in which
  `client start` has run), gw reaches the backend over SSH through the stanza `connect add`
  wrote, and `--version` agrees on the control side, the client host and the backend. Then
  `rm`, unless `--keep` or a check failed, when the backend stays for inspection.
- `test host`: seconds, no VM: the client host's drift check and client canary.
- `test backend PROJECT`: seconds, on an existing backend: its status and backend canary.

Each check prints one line, ok or FAIL with a detail, then a summary; the exit status is 1 when
any check failed. The canary runs with `--json` and its probes are compared here with this
package's golden result (canary_expected.json), so the expectations are the checkout's even
when the machine's package is older.
"""

import argparse
import dataclasses
import datetime
import sys
import time

from sandboxed_devenv import (
    canary,
    common,
    config,
    control_backends,
    control_images,
    control_inventory,
    remote,
)
from sandboxed_devenv.control_driver import DriverError
from sandboxed_devenv.control_inventory import InventoryError
from sandboxed_devenv.remote import RemoteError
from sandboxed_devenv.render import RenderError

PROJECT_PREFIX = "ci-"
GW_USER = "gw"
ERRORS = (RemoteError, DriverError, RenderError, InventoryError, config.SecretError, OSError)


@dataclasses.dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        status = "ok  " if self.ok else "FAIL"
        retval = f"{status} {self.name}" + (f": {self.detail}" if self.detail else "")
        if not self.ok:
            retval = common.warning(retval)
        return retval


def register(subparsers):
    test = common.add_group(subparsers, "test", "harnesses against real machines; never in GitHub CI (control)")

    end_to_end = test.add_parser(
        "end-to-end",
        help="all three roles on real VMs: image, a throwaway project, canary both roles, SSH path, versions, teardown",
    )
    end_to_end.add_argument("--keep", action="store_true", help="leave the throwaway backend in place afterwards")
    end_to_end.add_argument("--cpus", metavar="N", type=int, help="CPUs for the throwaway backend (default: the backend default)")
    end_to_end.add_argument("--memory-gib", metavar="N", type=int, help="memory for it in GiB (default: the backend default)")
    end_to_end.set_defaults(handler=cmd_end_to_end)

    host = test.add_parser("host", help="seconds, no VM: `host install --check` and the client canary on the client host")
    host.set_defaults(handler=cmd_host)

    backend = test.add_parser("backend", help="seconds, on an existing backend: `backend status` and the backend canary")
    backend.add_argument("project", metavar="PROJECT")
    backend.set_defaults(handler=cmd_backend)


# --- the checks ----------------------------------------------------------------------------------


def canary_check(machine, role: str, arguments: list[str]) -> Check:
    """The canary with `--json` on a machine, read back and compared here with this package's
    golden result for the role, so the expectations are the checkout's even when the far side's
    package is older: ok when nothing failed and every expectation was met."""
    completed = machine.run([machine.command_path, *arguments, "--json"], capture=True, check=False)
    report = canary.parse_json_lines(completed.stdout or "")
    counts = report["summary"]
    if counts is None:
        stderr = (completed.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else (report["other"][-1] if report["other"] else f"exit status {completed.returncode}, no report")
        retval = Check(f"canary {role}", False, detail)
    else:
        mismatches = canary.compare(report["probes"], canary.expected_for(role))
        detail = f"{counts['pass']} pass, {counts['fail']} fail, {counts['info']} info"
        if mismatches:
            detail += "; " + "; ".join(mismatches)
        retval = Check(f"canary {role}", counts["fail"] == 0 and not mismatches, detail)
    return retval


def version_of(machine) -> str:
    completed = machine.run([machine.command_path, "--version"], capture=True, check=False)
    retval = (completed.stdout or "").strip() or f"unknown (exit status {completed.returncode})"
    return retval


def backend_checks(backend: remote.Remote, project: str) -> list[Check]:
    retlist = []
    try:
        status = backend.status()
    except RemoteError as error:
        retlist.append(Check(f"backend {project} status", False, str(error)))
        return retlist
    retlist.append(Check(f"backend {project} ready", bool(status.get("ready")),
                         f"cloud-init {status.get('cloud_init', {}).get('status')}, package {status.get('package_version')}"))
    ide = status.get("ide_backend") or {}
    installed = ide.get("installed") or {}
    retlist.append(Check("IDE backend registered for Gateway", bool(installed) and bool(ide.get("registered")),
                         f"{installed.get('name')} {installed.get('version')}" if installed else "not installed"))
    retlist.append(canary_check(backend, "backend", ["canary", "--role", "backend"]))
    return retlist


def host_checks(client) -> list[Check]:
    retlist = []
    completed = client.run([client.command_path, "host", "install", "--check"], capture=True, check=False)
    if hasattr(client, "host"):
        completed = client.run(["sudo", client.command_path, "host", "install", "--check"], capture=True, check=False)
    lines = (completed.stdout or "").strip().splitlines()
    summary = lines[-1] if lines else (completed.stderr or "").strip().splitlines()[-1:] or ["no output"]
    if isinstance(summary, list):
        summary = summary[0]
    retlist.append(Check("client host install --check", completed.returncode == 0, summary))
    retlist.append(canary_check(client, "client", ["host", "check"]))
    return retlist


def ssh_path_check(client, project: str, address: str) -> Check:
    """gw on the client host reaches the backend through the stanza `connect add` wrote."""
    completed = client.run(["sudo", "-u", GW_USER, "ssh", "-o", "BatchMode=yes", project, "hostname"], capture=True, check=False)
    hostname = (completed.stdout or "").strip()
    if completed.returncode == 0:
        retval = Check(f"gw on the client host reaches {project}", True, f"{hostname} at {address}")
    else:
        detail = (completed.stderr or "").strip().splitlines()
        retval = Check(f"gw on the client host reaches {project}", False, detail[-1] if detail else f"exit status {completed.returncode}")
    return retval


def version_checks(client, backend: remote.Remote | None) -> list[Check]:
    control = common.package_version()
    machines = {"control side": control, "client host": version_of(client).replace("sandboxed-devenv ", "")}
    if backend is not None:
        machines["backend"] = version_of(backend).replace("sandboxed-devenv ", "")
    distinct = set(machines.values())
    detail = ", ".join(f"{where} {version}" for where, version in machines.items())
    retlist = [Check("package version agrees", len(distinct) == 1, detail)]
    return retlist


def print_checks(command: str, checks: list[Check]) -> int:
    failed = 0
    for check in checks:
        print(f"{command}: {check.line()}", flush=True)
        if not check.ok:
            failed += 1
    print(f"{command}: {len(checks) - failed} ok, {failed} failed")
    retval = failed
    return retval


# --- commands -------------------------------------------------------------------------------------


def test_host(args) -> int:
    command = "test host"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    client = remote.client_host(user_config, runner)
    if args.dry_run:
        print(f"would run `sudo sandboxed-devenv host install --check` and `sandboxed-devenv host check --json` "
              f"on the client host ({client.target}) and compare the canary with this package's golden result")
        retval = 0
        return retval
    checks = host_checks(client) + version_checks(client, None)
    retval = 1 if print_checks(command, checks) else 0
    return retval


def cmd_host(args) -> int:
    try:
        retval = test_host(args)
    except ERRORS as error:
        retval = remote.report(error, "test host")
    return retval


def test_backend(args) -> int:
    command = "test backend"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    record = control_inventory.require(args.project)
    if args.dry_run:
        print(f"would run `backend status --json` and `canary --role backend --json` on {record.vm_name} "
              f"at {record.address} and compare the canary with this package's golden result")
        retval = 0
        return retval
    if record.removed or not record.address:
        raise InventoryError(f"{args.project}'s backend is gone; only its record remains")
    backend = remote.backend(record.address, record.vm_name, runner)
    checks = backend_checks(backend, args.project) + version_checks_backend_only(backend)
    retval = 1 if print_checks(command, checks) else 0
    return retval


def version_checks_backend_only(backend: remote.Remote) -> list[Check]:
    control = common.package_version()
    far = version_of(backend).replace("sandboxed-devenv ", "")
    retlist = [Check("package version agrees", control == far, f"control side {control}, backend {far}")]
    return retlist


def cmd_backend(args) -> int:
    try:
        retval = test_backend(args)
    except ERRORS as error:
        retval = remote.report(error, "test backend")
    return retval


def end_to_end(args) -> int:
    command = "test end-to-end"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    project = f"{PROJECT_PREFIX}{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d%H%M%S')}"
    if args.dry_run:
        print(f"would run `image build` (a no-op when the image is current), `new {project} --disposable`, the backend "
              f"and client host checks, gw's SSH path, the version comparison, then `rm {project} --yes`"
              f"{' (kept with --keep)' if args.keep else ''}")
        retval = 0
        return retval
    started = time.monotonic()
    checks = []
    print(f"{command}: step 1 of 4: the base image", flush=True)
    status = control_images.cmd_build(argparse.Namespace(dry_run=False, verbose=args.verbose, force=False, wheel=None,
                                                          cpus=args.cpus, memory_gib=args.memory_gib))
    checks.append(Check("base image current or built", status == 0))
    if status != 0:
        print_checks(command, checks)
        retval = 1
        return retval
    print(f"{command}: step 2 of 4: a throwaway project, {project}", flush=True)
    status = control_backends.cmd_new(argparse.Namespace(dry_run=False, verbose=args.verbose, project=project, repo=[],
                                                          disposable=True, deploy_key_grants_repo_write=False,
                                                          cpus=args.cpus, memory_gib=args.memory_gib))
    checks.append(Check(f"new {project}", status == 0))
    record = control_inventory.load(project)
    if status != 0 or record is None:
        print_checks(command, checks)
        print(f"{command}: {project} is left as it is; `sandboxed-devenv rm {project} --yes` removes it", file=sys.stderr)
        retval = 1
        return retval
    print(f"{command}: step 3 of 4: the checks", flush=True)
    backend = remote.backend(record.address, record.vm_name, runner)
    client = remote.client_host(user_config, runner)
    checks += backend_checks(backend, project)
    checks += host_checks(client)
    checks.append(ssh_path_check(client, project, record.address))
    checks += version_checks(client, backend)
    failed = print_checks(command, checks)
    print(f"{command}: step 4 of 4: teardown", flush=True)
    if args.keep:
        print(f"{command}: {project} kept (--keep); `sandboxed-devenv rm {project} --yes` removes it")
    elif failed:
        print(common.warning(f"{command}: {project} kept for inspection after {failed} failed check(s); "
                             f"`sandboxed-devenv rm {project} --yes` removes it"), file=sys.stderr)
    else:
        status = control_backends.cmd_rm(argparse.Namespace(dry_run=False, verbose=args.verbose, project=project, yes=True))
        if status != 0:
            failed += 1
            print(common.warning(f"{command}: rm {project} failed with status {status}"), file=sys.stderr)
    print(f"{command}: {'passed' if not failed else 'failed'}; total {common.format_duration(time.monotonic() - started)}")
    retval = 1 if failed else 0
    return retval


def cmd_end_to_end(args) -> int:
    try:
        retval = end_to_end(args)
    except ERRORS as error:
        retval = remote.report(error, "test end-to-end")
    return retval
