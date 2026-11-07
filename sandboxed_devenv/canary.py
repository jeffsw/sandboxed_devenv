"""`sandboxed-devenv canary`: the sandbox probes for the client or backend role, run locally.

M1 moves rehearsal/canary.py here. It stays a stdlib-only module so `python3 -m
sandboxed_devenv.canary` works on a VM whose install is half done.
"""

from sandboxed_devenv import common


def register(subparsers):
    parser = subparsers.add_parser(
        "canary",
        help="the sandbox probes for the client or backend role, run locally (any role)",
    )
    parser.add_argument("--role", choices=["client", "backend"], required=True, help="which role's probes to run")
    parser.add_argument("--json", action="store_true", help="one JSON object per probe plus a summary")
    parser.set_defaults(handler=cmd_canary)


def cmd_canary(args) -> int:
    retval = common.not_implemented("canary", "M1")
    return retval
