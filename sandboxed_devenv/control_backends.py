"""One backend per project: new, ls, start, stop, rm, clone, resources, ssh, serial-console (control, M2).

Every command takes the project's name and finds the backend through the inventory; no
command takes a backend name (PLAN.md, "Projects and repositories").
"""

import argparse

from sandboxed_devenv import common


def _project_argument(parser: argparse.ArgumentParser, metavar: str = "PROJECT") -> None:
    parser.add_argument("project", metavar=metavar, help="the project; its backend is found through the inventory")


def _resource_arguments(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--cpus", metavar="N", type=int, help=f"CPUs for the backend: {what}")
    parser.add_argument("--memory-gib", metavar="N", type=int, help=f"memory for the backend, in GiB: {what}")


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

    ls = subparsers.add_parser("ls", help="projects with their backend's state, address, repositories and ages (control)")
    ls.set_defaults(handler=cmd_ls)

    start = subparsers.add_parser("start", help="start the project's backend (control)")
    _project_argument(start)
    start.set_defaults(handler=cmd_start)

    stop = subparsers.add_parser("stop", help="stop the project's backend (control)")
    _project_argument(stop)
    stop.set_defaults(handler=cmd_stop)

    rm = subparsers.add_parser(
        "rm",
        help="revoke every credential the inventory lists, remove the connection, delete the backend (control)",
    )
    _project_argument(rm)
    rm.set_defaults(handler=cmd_rm)

    clone = subparsers.add_parser(
        "clone",
        help="a disposable backend for NEW_PROJECT copied from PROJECT's, with fresh keys (control)",
    )
    _project_argument(clone)
    clone.add_argument("new_project", metavar="NEW_PROJECT", help="the new, short-lived project")
    clone.set_defaults(handler=cmd_clone)

    resources = subparsers.add_parser(
        "resources",
        help="show or change the backend's CPUs and memory; asks before a stop and start when one is needed (control)",
    )
    _project_argument(resources)
    _resource_arguments(resources, "the new value; without either flag the current values are shown")
    resources.add_argument("--yes", action="store_true", help="approve the stop and start without asking, for scripts")
    resources.set_defaults(handler=cmd_resources)

    ssh = subparsers.add_parser("ssh", help="a shell as `dev` in the project's backend with the rig key (control)")
    _project_argument(ssh)
    ssh.add_argument("cmd", metavar="CMD", nargs=argparse.REMAINDER, help="a command instead of a shell")
    ssh.set_defaults(handler=cmd_ssh)

    serial_console = subparsers.add_parser(
        "serial-console",
        help="the backend's serial console through the driver, for when SSH is not up (control)",
    )
    _project_argument(serial_console)
    serial_console.set_defaults(handler=cmd_serial_console)


def cmd_new(args) -> int:
    retval = common.not_implemented("new", "M2")
    return retval


def cmd_ls(args) -> int:
    retval = common.not_implemented("ls", "M2")
    return retval


def cmd_start(args) -> int:
    retval = common.not_implemented("start", "M2")
    return retval


def cmd_stop(args) -> int:
    retval = common.not_implemented("stop", "M2")
    return retval


def cmd_rm(args) -> int:
    retval = common.not_implemented("rm", "M2")
    return retval


def cmd_clone(args) -> int:
    retval = common.not_implemented("clone", "M2")
    return retval


def cmd_resources(args) -> int:
    retval = common.not_implemented("resources", "M2")
    return retval


def cmd_ssh(args) -> int:
    retval = common.not_implemented("ssh", "M2")
    return retval


def cmd_serial_console(args) -> int:
    retval = common.not_implemented("serial-console", "M2")
    return retval
