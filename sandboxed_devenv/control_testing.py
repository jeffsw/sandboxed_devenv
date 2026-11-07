"""`sandboxed-devenv test ...`: the harnesses that run against real machines (control role, M4)."""

from sandboxed_devenv import common


def register(subparsers):
    test = common.add_group(subparsers, "test", "harnesses against real machines; never in GitHub CI (control)")

    end_to_end = test.add_parser(
        "end-to-end",
        help="all three roles on real VMs: image, a throwaway project, canary both roles, SSH path, versions, teardown",
    )
    end_to_end.set_defaults(handler=cmd_end_to_end)

    host = test.add_parser("host", help="seconds, no VM: `host install --check` and the client canary on the client host")
    host.set_defaults(handler=cmd_host)

    backend = test.add_parser("backend", help="seconds, on an existing backend: `backend status` and the backend canary")
    backend.add_argument("project", metavar="PROJECT")
    backend.set_defaults(handler=cmd_backend)


def cmd_end_to_end(args) -> int:
    retval = common.not_implemented("test end-to-end", "M4")
    return retval


def cmd_host(args) -> int:
    retval = common.not_implemented("test host", "M4")
    return retval


def cmd_backend(args) -> int:
    retval = common.not_implemented("test backend", "M4")
    return retval
