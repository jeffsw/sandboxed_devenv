"""Small helpers shared by the role modules: the package version, subcommand groups, stubs."""

import argparse
import importlib.metadata
import platform
import shutil
import sys

DIST_NAME = "sandboxed-devenv"
COMMAND_NAME = "sandboxed-devenv"
# Where `sudo pipx install --global` puts the command on Linux: inside sudo's secure_path and
# readable by every user, so the client host's `sudo --user=gw` reaches it without PATH tricks.
GLOBAL_COMMAND_PATH = "/usr/local/bin/sandboxed-devenv"


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
