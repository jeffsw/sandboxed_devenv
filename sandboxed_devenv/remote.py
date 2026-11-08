"""Run `sandboxed-devenv` on the client host or on a backend, over SSH or locally (control role).

The control side never copies configuration into a VM by hand: it runs the same command there
(PLAN.md, "Roles"). A ``Remote`` reaches one machine as one user with the rig key and nothing
else: no agent, no user ssh_config (``-F none``), host keys in a file of the caller's choosing,
where the first key seen is accepted and any later change refused. Pinning the key before the
first connection is M3's job (`connect add`); until then the file is per VM and starts empty.

``push`` copies a file the same way, and ``install_wheel`` pushes a wheel and runs
``sudo pipx install --global`` on it (PLAN.md, "Installation"). ``wait_for_ssh`` and
``wait_ready`` are the two waits every VM creation needs: the first login, and
``backend status --json`` reporting ``ready``. ``Local`` is the same interface for a client
host that is this machine (``client_host: local``).

No shell expansion. A command is a list of arguments; ``shlex.join`` quotes every one of them
for the far side's login shell, so ``~``, ``$HOME`` and globs arrive as the literal characters.
Paths are given in full (``/tmp/x.whl``, ``/home/dev/...``). ``Local`` keeps the same rule, so
a caller cannot tell the two apart by what expands.
"""

import dataclasses
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse

from sandboxed_devenv import common, config

BACKEND_USER = "dev"
DEFAULT_SSH_PORT = 22
REMOTE_TEMP_DIR = "/tmp"
SSH_CONNECT_TIMEOUT = 5
# `ssh -F none` reads no configuration file, so a user's own stanzas (agent forwarding, a
# ProxyJump, a different key) never reach a VM. Every option is given here.
SSH_OPTIONS = [
    "-F", "none",
    "-o", "IdentitiesOnly=yes",
    "-o", "IdentityAgent=none",
    "-o", "ForwardAgent=no",
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "LogLevel=ERROR",
]


class RemoteError(Exception):
    pass


@dataclasses.dataclass
class Remote:
    """One machine reached over SSH as one user with the rig key."""

    user: str
    host: str
    key: pathlib.Path
    known_hosts: pathlib.Path
    port: int = DEFAULT_SSH_PORT
    # The package's command on the far side; the global pipx install's path on every Linux VM.
    command_path: str = common.GLOBAL_COMMAND_PATH
    runner: common.Runner = dataclasses.field(default_factory=common.Runner)

    @property
    def target(self) -> str:
        retval = f"{self.user}@{self.host}"
        return retval

    def options(self) -> list[str]:
        retlist = list(SSH_OPTIONS)
        retlist += ["-o", f"UserKnownHostsFile={self.known_hosts}", "-i", str(self.key)]
        return retlist

    def ssh_command(self, remote_command: list[str] | None = None, tty: bool = False) -> list[str]:
        """``ssh`` with every option, the target, and the command quoted for the remote shell.
        No shell expansion."""
        retlist = ["ssh", *self.options(), "-p", str(self.port)]
        if tty:
            retlist.append("-t")
        retlist.append(self.target)
        if remote_command:
            retlist.append(shlex.join(remote_command))
        return retlist

    def scp_command(self, sources: list[pathlib.Path], remote_path: str) -> list[str]:
        retlist = ["scp", "-q", *self.options(), "-P", str(self.port)]
        for source in sources:
            retlist.append(str(source))
        retlist.append(f"{self.target}:{remote_path}")
        return retlist

    def run(self, remote_command: list[str], check: bool = True, capture: bool = False,
            timeout: float | None = None, quiet: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
        """The command on the far side; ``quiet`` discards its output when nothing is captured;
        ``input`` is fed to its standard input (a file's content through `sudo tee`, so nothing
        secret lands in a temporary file). No shell expansion."""
        kwargs = {}
        if capture:
            kwargs["capture_output"] = True
        elif quiet:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if timeout is not None:
            kwargs["timeout"] = timeout
        if input is not None:
            kwargs["input"] = input
        retval = self.runner.run(self.ssh_command(remote_command), **kwargs)
        if check and retval.returncode != 0:
            detail = (retval.stderr or "").strip() if capture else ""
            raise RemoteError(f"{shlex.join(remote_command)} on {self.target} failed with status {retval.returncode}"
                              + (f": {detail}" if detail else ""))
        return retval

    def tool(self, args: list[str], sudo: bool = False, parse_json: bool = False, check: bool = True,
             json_flag: bool = True):
        """`sandboxed-devenv ARGS` on the far side; with ``parse_json`` the output parsed as JSON,
        after adding ``--json`` to the command unless ``json_flag`` is off (for the `backend repo`
        commands, which print JSON and take no flag)."""
        remote_command = [self.command_path, *args]
        if sudo:
            remote_command = ["sudo", *remote_command]
        if parse_json:
            if json_flag and "--json" not in args:
                remote_command.append("--json")
            completed = self.run(remote_command, check=check, capture=True)
            text = completed.stdout or ""
            if self.runner.dry_run:
                retval = {}
            else:
                try:
                    retval = json.loads(text)
                except json.JSONDecodeError as error:
                    raise RemoteError(f"{shlex.join(remote_command)} on {self.target} printed no JSON: {error}: "
                                      f"{text[:200]!r}") from error
        else:
            retval = self.run(remote_command, check=check)
        return retval

    def push(self, sources: list[pathlib.Path], remote_path: str) -> None:
        completed = self.runner.run(self.scp_command(sources, remote_path))
        if completed.returncode != 0:
            raise RemoteError(f"scp to {self.target}:{remote_path} failed with status {completed.returncode}")

    def install_wheel(self, wheel: pathlib.Path) -> str:
        """Push the wheel and install it globally; returns the far side's `--version` line
        (PLAN.md, "Installation": pipx on Linux, the venv under /opt/pipx). The wheel lands under
        /tmp by an explicit path: the remote command is quoted for the far shell, so a `~` in it
        would never be expanded (found 8 November 2026, the first `image build`)."""
        remote_wheel = f"{REMOTE_TEMP_DIR}/{wheel.name}"
        self.push([wheel], remote_wheel)
        self.run(["sudo", "pipx", "install", "--global", "--force", remote_wheel], capture=True)
        self.run(["rm", "-f", remote_wheel], check=False, quiet=True)
        completed = self.run([self.command_path, "--version"], capture=True)
        retval = (completed.stdout or "").strip()
        return retval

    def reachable(self) -> bool:
        """One login attempt; the host key is learned on the first success."""
        completed = self.runner.run(self.ssh_command(["true"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        retval = completed.returncode == 0
        return retval

    def wait_for_ssh(self, timeout: float, interval: float = 5.0) -> float:
        """Seconds until the first successful login; RemoteError after ``timeout``."""
        started = time.monotonic()
        while True:
            if self.reachable():
                break
            if time.monotonic() - started > timeout:
                raise RemoteError(f"no SSH login to {self.target} within {common.format_duration(timeout)}")
            time.sleep(interval)
        retval = time.monotonic() - started
        return retval

    def wait_for_cloud_init(self, timeout: float) -> str:
        """`cloud-init status --wait` on the far side; returns its last line, which says done or
        error; RemoteError when it is not done."""
        completed = self.run(["cloud-init", "status", "--wait"], check=False, capture=True, timeout=timeout)
        lines = (completed.stdout or "").strip().splitlines()
        retval = lines[-1].strip() if lines else ""
        if self.runner.dry_run:
            retval = "status: done"
        elif "done" not in retval:
            detail = self.run(["cloud-init", "status", "--long"], check=False, capture=True).stdout or ""
            raise RemoteError(f"cloud-init on {self.target} did not finish cleanly: {retval or completed.stderr}\n{detail}")
        return retval

    def status(self) -> dict:
        """`backend status --json` from a backend."""
        retval = self.tool(["backend", "status"], parse_json=True)
        return retval

    def wait_ready(self, timeout: float, interval: float = 10.0) -> dict:
        """Poll `backend status --json` until ``ready``; RemoteError after ``timeout``."""
        started = time.monotonic()
        while True:
            try:
                report = self.status()
            except RemoteError:
                report = {}
            if report.get("ready") or self.runner.dry_run:
                break
            if time.monotonic() - started > timeout:
                raise RemoteError(f"{self.target} did not report ready within {common.format_duration(timeout)}; "
                                  f"last status: {json.dumps(report)[:400]}")
            time.sleep(interval)
        retval = report
        return retval


@dataclasses.dataclass
class Local:
    """The client host when it is this machine: the same calls, run locally. No shell expansion."""

    command_path: str = dataclasses.field(default_factory=common.command_path)
    runner: common.Runner = dataclasses.field(default_factory=common.Runner)

    @property
    def target(self) -> str:
        retval = "local"
        return retval

    def run(self, command: list[str], check: bool = True, capture: bool = False, timeout: float | None = None,
            quiet: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
        kwargs = {}
        if capture:
            kwargs["capture_output"] = True
        elif quiet:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if timeout is not None:
            kwargs["timeout"] = timeout
        if input is not None:
            kwargs["input"] = input
        retval = self.runner.run(command, **kwargs)
        if check and retval.returncode != 0:
            raise RemoteError(f"{shlex.join(command)} failed with status {retval.returncode}")
        return retval

    def tool(self, args: list[str], sudo: bool = False, parse_json: bool = False, check: bool = True):
        command = [self.command_path, *args]
        if sudo:
            command = ["sudo", *command]
        if parse_json:
            if "--json" not in args:
                command.append("--json")
            completed = self.run(command, check=check, capture=True)
            retval = {} if self.runner.dry_run else json.loads(completed.stdout or "{}")
        else:
            retval = self.run(command, check=check)
        return retval

    def push(self, sources: list[pathlib.Path], remote_path: str) -> None:
        dest = pathlib.Path(remote_path)
        for source in sources:
            if self.runner.dry_run:
                print(f"would copy {source} to {dest}")
            else:
                shutil.copy2(source, dest)


def rig_key_path() -> pathlib.Path:
    """The rig key's private half in the secrets directory; the control machine's way into every VM."""
    retval = config.secrets_dir() / config.RIG_KEY_NAME
    return retval


def require_rig_key() -> pathlib.Path:
    """The rig key, or RemoteError naming the file and what creates it."""
    retval = rig_key_path()
    if not retval.is_file():
        raise RemoteError(f"no rig key at {retval}; `sandboxed-devenv image build` creates the pair on first use")
    return retval


def parse_client_host(spec: str) -> tuple[str, str, int] | None:
    """``ssh://user@host[:port]`` as (user, host, port), or None for ``local``."""
    retval = None
    if spec != "local":
        parsed = urllib.parse.urlsplit(spec)
        if parsed.scheme != "ssh" or not parsed.hostname or not parsed.username:
            raise RemoteError(f"client_host must be `local` or `ssh://user@host[:port]`, not {spec!r}")
        retval = (parsed.username, parsed.hostname, parsed.port or DEFAULT_SSH_PORT)
    return retval


def known_hosts_dir() -> pathlib.Path:
    """One known_hosts file per machine the control side talks to, named after the VM or host."""
    retval = config.home_dir() / "known_hosts"
    return retval


def known_hosts_file(name: str) -> pathlib.Path:
    retval = known_hosts_dir() / name
    return retval


def forget_host_keys(name: str) -> None:
    """Drop what was learned about a VM, before it is rebuilt at the same address."""
    path = known_hosts_file(name)
    if path.exists():
        path.unlink()


def client_host(user_config: config.UserConfig, runner: common.Runner | None = None) -> Remote | Local:
    """The client host as the user config names it."""
    runner = runner or common.Runner()
    parsed = parse_client_host(user_config.client_host)
    if parsed is None:
        retval = Local(runner=runner)
    else:
        user, host, port = parsed
        known_hosts_dir().mkdir(parents=True, exist_ok=True)
        retval = Remote(user=user, host=host, key=require_rig_key(), known_hosts=known_hosts_file("client-host"),
                        port=port, runner=runner)
    return retval


def backend(address: str, vm_name: str, runner: common.Runner | None = None) -> Remote:
    """A backend as `dev` at its address, with the rig key and the VM's own known_hosts file."""
    known_hosts_dir().mkdir(parents=True, exist_ok=True)
    retval = Remote(user=BACKEND_USER, host=address, key=require_rig_key(), known_hosts=known_hosts_file(vm_name),
                    runner=runner or common.Runner())
    return retval


def report(error: Exception, command: str) -> int:
    print(f"sandboxed-devenv {command}: {error}", file=sys.stderr)
    retval = 1
    return retval
