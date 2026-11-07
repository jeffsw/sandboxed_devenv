"""Small helpers shared by the role modules: the package version, subcommand groups, stubs."""

import argparse
import importlib.metadata
import sys

DIST_NAME = "sandboxed-devenv"


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
