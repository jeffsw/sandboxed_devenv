"""`sandboxed-devenv client ...`: the sandboxed IDE client, what gw-run, gw-session and gw-exec did (M1)."""

import argparse

from sandboxed_devenv import common


def register(subparsers):
    client = common.add_group(subparsers, "client", "the sandboxed IDE client as the gw user (client host)")

    start = client.add_parser("start", help="the runtime-dir and Wayland socket ACLs, then `client session` as gw via sudo")
    start.set_defaults(handler=cmd_start)

    session = client.add_parser("session", help="as gw: the private Xwayland with its cookie, then the Gateway launcher")
    session.set_defaults(handler=cmd_session)

    exec_ = client.add_parser("exec", help="run a command as gw with its display and cookie")
    exec_.add_argument("cmd", metavar="CMD", nargs=argparse.REMAINDER, help="the command, after `--`")
    exec_.set_defaults(handler=cmd_exec)


def cmd_start(args) -> int:
    retval = common.not_implemented("client start", "M1")
    return retval


def cmd_session(args) -> int:
    retval = common.not_implemented("client session", "M1")
    return retval


def cmd_exec(args) -> int:
    retval = common.not_implemented("client exec", "M1")
    return retval
