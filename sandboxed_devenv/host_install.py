"""`sandboxed-devenv host ...`: provision and check the client host (client host role, M1)."""

from sandboxed_devenv import common


def register(subparsers):
    host = common.add_group(subparsers, "host", "provision and check the client host; Linux only (client host)")

    install = host.add_parser(
        "install",
        help="the gw user and linger, files from the manifest, AppArmor, sysctl, nftables, the Gateway tarball; needs root",
    )
    install.add_argument("--check", action="store_true", help="exit non-zero if anything would change; drift check")
    install.set_defaults(handler=cmd_install)

    check = host.add_parser("check", help="run the canary in the client role as gw and print the summary")
    check.set_defaults(handler=cmd_check)


def cmd_install(args) -> int:
    retval = common.not_implemented("host install", "M1")
    return retval


def cmd_check(args) -> int:
    retval = common.not_implemented("host check", "M1")
    return retval
