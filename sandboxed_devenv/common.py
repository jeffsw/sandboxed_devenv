"""Small helpers shared by the role modules: the package version, subcommand groups, stubs, the
machine's architecture as versions.yaml keys it, a command runner with --dry-run and --verbose,
and the announcement every slow step makes (PLAN.md, "Principles": say what, say why, report
the elapsed time, draw no progress bars)."""

import argparse
import contextlib
import dataclasses
import importlib.metadata
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time

DIST_NAME = "sandboxed-devenv"
COMMAND_NAME = "sandboxed-devenv"
# `always`, `never` or unset (a terminal decides). The control side sets it to `always` for a
# command it runs on another machine when its own output is a terminal, since the far side only
# sees a pipe; older versions there ignore the variable. NO_COLOR is honoured.
COLOR_ENV_VAR = "SANDBOXED_DEVENV_COLOR"
BOLD_YELLOW = "\033[1;33m"
RESET = "\033[0m"
# Where `sudo pipx install --global` puts the command on Linux: inside sudo's secure_path and
# readable by every user, so the client host's `sudo --user=gw` reaches it without PATH tricks.
GLOBAL_COMMAND_PATH = "/usr/local/bin/sandboxed-devenv"


def color_wanted(stream=None) -> bool:
    """Whether to colour output: the environment's say, else whether the stream is a terminal."""
    setting = os.environ.get(COLOR_ENV_VAR, "").lower()
    if setting == "always":
        retval = True
    elif setting == "never" or os.environ.get("NO_COLOR"):
        retval = False
    else:
        stream = stream or sys.stdout
        retval = bool(getattr(stream, "isatty", lambda: False)())
    return retval


def warning(text: str, stream=None) -> str:
    """A warning-like line in bold yellow where a terminal shows it, plain otherwise."""
    retval = f"{BOLD_YELLOW}{text}{RESET}" if color_wanted(stream) else text
    return retval


def package_version() -> str:
    """The version from the installed metadata; pyproject.toml is the one place it is set."""
    try:
        retval = importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        retval = "unknown (not installed)"
    return retval


def add_group(subparsers: argparse._SubParsersAction, name: str, help_text: str) -> argparse._SubParsersAction:
    """A subcommand group such as `host` or `backend`; returns the subparsers its members register on."""
    group = subparsers.add_parser(name, help=help_text, description=help_text)
    retval = group.add_subparsers(title="subcommands", dest=f"{name}_command", metavar="SUBCOMMAND", required=True)
    return retval


def not_implemented(command: str, milestone: str) -> int:
    """Exit status 1 and a pointer at the PLAN.md milestone that delivers the command."""
    print(f"sandboxed-devenv: `{command}` is not implemented yet; see PLAN.md milestone {milestone}", file=sys.stderr)
    retval = 1
    return retval


def command_path() -> str:
    """The command to run as another user or on another machine: the installed one when it is on
    the PATH, else where the global pipx install puts it."""
    retval = shutil.which(COMMAND_NAME) or GLOBAL_COMMAND_PATH
    return retval


def linux_only(command: str) -> bool:
    """False, with the reason on stderr, when a client-host or backend command runs elsewhere."""
    system = platform.system()
    retval = system == "Linux"
    if not retval:
        print(f"sandboxed-devenv: `{command}` runs on a Linux machine playing its role; this is {system}",
              file=sys.stderr)
    return retval


def architecture() -> str:
    """This machine's CPU architecture as versions.yaml keys downloads: macOS says arm64 where Linux
    says aarch64, and both spell x86_64 the same way."""
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        retval = "aarch64"
    elif machine in ("x86_64", "amd64"):
        retval = "x86_64"
    else:
        retval = machine
    return retval


def format_duration(seconds: float) -> str:
    """Whole seconds under two minutes, else minutes and seconds."""
    whole = int(round(seconds))
    if whole < 120:
        retval = f"{whole} s"
    else:
        retval = f"{whole // 60} min {whole % 60} s"
    return retval


@dataclasses.dataclass
class Runner:
    """Runs external commands for the control side. With ``dry_run`` it prints each command
    instead and returns an empty success, except a ``query``, a command that only reads (a
    driver listing its VMs, a bridge's address), which runs either way so the plan a dry run
    prints rests on the real state; with ``verbose`` it prints each command on stderr before
    running it. Tests replace it."""

    dry_run: bool = False
    verbose: bool = False

    def run(self, command: list[str], query: bool = False, **kwargs) -> subprocess.CompletedProcess:
        if self.dry_run and not query:
            print(f"would run: {shlex.join(command)}")
            retval = subprocess.CompletedProcess(command, 0, "", "")
        else:
            if self.verbose:
                print(f"+ {shlex.join(command)}", file=sys.stderr, flush=True)
            kwargs.setdefault("text", True)
            retval = subprocess.run(command, **kwargs)
        return retval


@contextlib.contextmanager
def slow_step(command: str, what: str, why: str):
    """Announce a step that takes more than a few seconds: what it does and why it is slow before
    it starts, the elapsed time when it ends."""
    print(f"{command}: {what}: {why}", flush=True)
    started = time.monotonic()
    yield
    print(f"{command}: {what}: done in {format_duration(time.monotonic() - started)}", flush=True)
