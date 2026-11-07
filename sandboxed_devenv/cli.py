"""One argparse tree for every machine; each role module registers its own subcommands.

Exit status: 0 ok, 1 failure (including a subcommand run on a machine that cannot play its
role), 2 usage error, which argparse reports itself.
"""

import argparse
import sys

from sandboxed_devenv import (
    backend_prepare,
    canary,
    common,
    control_backends,
    control_creds,
    control_images,
    control_testing,
    doctor,
    host_client,
    host_connect,
    host_install,
    render,
)

PROG = "sandboxed-devenv"
DESCRIPTION = (
    "Manage the sandboxed development environment: the IDE thin client in a sandbox on the "
    "client host, and one backend VM per project with its own credentials. The same command is "
    "installed on the control machine, the client host and every backend; `doctor` says which "
    "roles this machine can play."
)

# Registration order is the order `--help` lists the commands: any role first, then the control
# machine, the client host, and the backend VM (PLAN.md, "Roles").
ROLE_MODULES = [
    doctor,
    render,
    canary,
    control_images,
    control_backends,
    control_creds,
    control_testing,
    host_install,
    host_client,
    host_connect,
    backend_prepare,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"%(prog)s {common.package_version()}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what a mutating subcommand would do, without doing it",
    )
    parser.add_argument("--verbose", action="store_true", help="more detail on stderr")
    subparsers = parser.add_subparsers(title="commands", dest="command", metavar="COMMAND", required=True)
    for module in ROLE_MODULES:
        module.register(subparsers)
    retval = parser
    return retval


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error("a subcommand is required")
    retval = handler(args)
    return retval


if __name__ == "__main__":
    sys.exit(main())
