"""`sandboxed-devenv connect ...`: Gateway's connection entries and gw's ssh_config on the client host (M2)."""

from sandboxed_devenv import common


def register(subparsers):
    connect = common.add_group(
        subparsers, "connect", "Gateway's sshConfigs.xml entry, gw's ssh_config stanza, the pinned host key (client host)"
    )

    add = connect.add_parser("add", help="write the entry, the stanza and the pinned key for a project's backend")
    add.add_argument("project", metavar="PROJECT")
    add.add_argument("address", metavar="ADDRESS", help="the backend's address")
    add.add_argument("hostkey", metavar="HOSTKEY", help="the backend's SSH host public key, pinned in known_hosts")
    add.set_defaults(handler=cmd_add)

    rm = connect.add_parser("rm", help="remove a project's entry, stanza and pinned key")
    rm.add_argument("project", metavar="PROJECT")
    rm.set_defaults(handler=cmd_rm)

    ls = connect.add_parser("ls", help="the connections this client host knows")
    ls.set_defaults(handler=cmd_ls)


def cmd_add(args) -> int:
    retval = common.not_implemented("connect add", "M2")
    return retval


def cmd_rm(args) -> int:
    retval = common.not_implemented("connect rm", "M2")
    return retval


def cmd_ls(args) -> int:
    retval = common.not_implemented("connect ls", "M2")
    return retval
