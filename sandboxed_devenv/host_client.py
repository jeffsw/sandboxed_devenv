"""`sandboxed-devenv client ...`: the sandboxed IDE client as the gw user, on the client host.

What the rehearsal's gw-run, gw-session and gw-exec scripts did (summary 4.9, proven on desk on
9 October 2026), as subcommands:

- `client start`, as the desktop user inside the GNOME session: lends the session's Wayland
  socket to gw (an ACL on the socket and on the runtime directory, an empty ACL entry on
  everything else in it), then becomes gw through sudo and runs `client session`.
- `client session`, as gw: a private rootless Xwayland on DISPLAY :9 with an xauth cookie, for
  the client's embedded Chromium, then the Gateway launcher replaces the process.
- `client exec -- CMD`, as the desktop user: one command as gw with the same display
  environment; the canary, for one: `client exec -- sandboxed-devenv canary --role client`.

Dated gotchas, kept from the scripts:

- No login shell (9 October 2026): `sudo --login` passes the command through `bash -c`, which
  expands every $variable before gw runs it; Xwayland got an empty display argument that way.
  Plain `sudo env NAME=value ...` sets the environment without a second parse.
- XAUTHORITY is dropped on the way to gw because sudo passes the desktop user's through and it
  names Mutter's unreadable file; `client session` sets gw's own, with a fresh cookie per server
  start, so the world-writable socket in /tmp admits nobody else.
- The Xwayland server stays up and is reused on the next run; an auto-terminate option once
  shut it between Chromium's first attempt and its retry (9 October 2026).
- Several sockets in the desktop user's runtime directory are world-writable, so the traversal
  ACL alone would expose them; an explicit empty entry for gw outranks the "other" bits. Only
  entries present now are covered: a socket created later is reachable until `client start`
  runs again (the canary's runtime-dir probe checks).
- FUSE mounts such as doc/ and gvfs/ reject ACLs; they are mode 0500, so gw cannot enter them
  regardless, and the error is ignored.
"""

import argparse
import os
import pathlib
import pwd
import secrets
import shlex
import subprocess
import sys
import time

from sandboxed_devenv import common

GW_USER = "gw"
GW_HOME = pathlib.Path("/home/gw")
GATEWAY_DIR = GW_HOME / "gateway"
GATEWAY_LAUNCHER = GATEWAY_DIR / "bin" / "gateway.sh"
# The private X display for the client's embedded Chromium (JCEF), itself a Wayland client of
# the desktop user's compositor; the host X sockets stay closed.
X_DISPLAY = ":9"
XAUTHORITY_FILE = GW_HOME / ".Xauthority-gw"
GW_UID_PLACEHOLDER = "<gw-uid>"


def register(subparsers):
    client = common.add_group(subparsers, "client", "the sandboxed IDE client as the gw user (client host)")

    start = client.add_parser(
        "start",
        help="inside the GNOME session: lend the Wayland socket to gw, then `client session` as gw through sudo",
    )
    start.add_argument("args", metavar="ARG", nargs=argparse.REMAINDER, help="arguments for the Gateway launcher, after `--`")
    start.set_defaults(handler=cmd_start)

    session = client.add_parser("session", help="as gw: the private Xwayland with its cookie, then the Gateway launcher")
    session.add_argument("args", metavar="ARG", nargs=argparse.REMAINDER, help="arguments for the Gateway launcher, after `--`")
    session.set_defaults(handler=cmd_session)

    exec_ = client.add_parser("exec", help="run a command as gw with its display and cookie")
    exec_.add_argument("cmd", metavar="CMD", nargs=argparse.REMAINDER, help="the command, after `--`")
    exec_.set_defaults(handler=cmd_exec)


def wayland_socket() -> tuple[pathlib.Path, pathlib.Path]:
    """The runtime directory and the session's Wayland socket: XDG_RUNTIME_DIR, else /run/user/<uid> as an
    SSH login gets, and WAYLAND_DISPLAY, default wayland-0, an absolute path standing alone."""
    runtime_dir = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    display = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if display.startswith("/"):
        sock = pathlib.Path(display)
    else:
        sock = runtime_dir / display
    retval = (runtime_dir, sock)
    return retval


def gw_uid(dry_run: bool = False) -> str:
    """gw's uid as a string; a placeholder on a dry run where the user does not exist."""
    try:
        retval = str(pwd.getpwnam(GW_USER).pw_uid)
    except KeyError:
        if not dry_run:
            raise LookupError(f"no user {GW_USER}; `sudo sandboxed-devenv host install` creates it") from None
        retval = GW_UID_PLACEHOLDER
    return retval


def lend_commands(runtime_dir: pathlib.Path, sock: pathlib.Path) -> list[list[str]]:
    """The setfacl commands that let gw reach the Wayland socket and nothing else in the runtime dir."""
    retlist = [
        # Traverse the directory (mode 0700): execute grants lookup, not listing.
        ["setfacl", "--modify", f"u:{GW_USER}:x", str(runtime_dir)],
        # Connect to the socket itself.
        ["setfacl", "--modify", f"u:{GW_USER}:rw", str(sock)],
    ]
    entries = []
    if runtime_dir.is_dir():
        entries = sorted(runtime_dir.iterdir())
    for entry in entries:
        if entry == sock:
            continue
        retlist.append(["setfacl", "--modify", f"u:{GW_USER}:-", str(entry)])
    return retlist


def is_closing_entry(command: list[str]) -> bool:
    """The empty-ACL commands may fail on FUSE mounts; the others may not."""
    retval = command[2] == f"u:{GW_USER}:-"
    return retval


def sudo_command(sock: pathlib.Path, uid: str, argv: list[str], xauthority: bool) -> list[str]:
    """Become gw with the display environment: the desktop user's compositor by absolute socket path,
    gw's own runtime directory and session type, the private X display, and for `exec` the cookie."""
    retlist = [
        "sudo",
        f"--user={GW_USER}",
        "env",
        "--unset=XAUTHORITY",
        f"HOME={GW_HOME}",
        f"USER={GW_USER}",
        f"LOGNAME={GW_USER}",
        "SHELL=/bin/bash",
        "LANG=C.UTF-8",
        f"WAYLAND_DISPLAY={sock}",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "XDG_SESSION_TYPE=wayland",
        f"DISPLAY={X_DISPLAY}",
    ]
    if xauthority:
        retlist.append(f"XAUTHORITY={XAUTHORITY_FILE}")
    retlist.extend(argv)
    return retlist


def without_separator(argv: list[str]) -> list[str]:
    """argparse keeps the `--` in a REMAINDER some of the time; the command never wants it."""
    retlist = list(argv)
    if retlist and retlist[0] == "--":
        retlist = retlist[1:]
    return retlist


def session_environment(args) -> tuple[pathlib.Path, pathlib.Path, str] | None:
    """The runtime dir, the socket and gw's uid, or None (with the reason printed) when this is not a
    graphical session or gw does not exist."""
    runtime_dir, sock = wayland_socket()
    retval = None
    if not sock.exists() and not args.dry_run:
        print(f"sandboxed-devenv: no Wayland socket at {sock}; run this inside the desktop session "
              "(XDG_RUNTIME_DIR and WAYLAND_DISPLAY say where the socket is)", file=sys.stderr)
        return retval
    try:
        uid = gw_uid(args.dry_run)
    except LookupError as error:
        print(f"sandboxed-devenv: {error}", file=sys.stderr)
        return retval
    retval = (runtime_dir, sock, uid)
    return retval


def cmd_start(args) -> int:
    if not args.dry_run and not common.linux_only("client start"):
        retval = 1
        return retval
    environment = session_environment(args)
    if environment is None:
        retval = 1
        return retval
    runtime_dir, sock, uid = environment
    commands = lend_commands(runtime_dir, sock)
    session = sudo_command(sock, uid, [common.command_path(), "client", "session", *without_separator(args.args)],
                           xauthority=False)
    if args.dry_run:
        for command in [*commands, session]:
            print(f"would run: {shlex.join(command)}")
        retval = 0
        return retval
    for command in commands:
        if is_closing_entry(command):
            subprocess.run(command, stderr=subprocess.DEVNULL)
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            print(f"sandboxed-devenv: {shlex.join(command)} failed", file=sys.stderr)
            retval = 1
            return retval
    os.execvp(session[0], session)


def xwayland_running() -> bool:
    completed = subprocess.run(["pgrep", "-u", GW_USER, "-x", "Xwayland"], stdout=subprocess.DEVNULL)
    retval = completed.returncode == 0
    return retval


def xwayland_command() -> list[str]:
    retlist = ["Xwayland", X_DISPLAY, "-rootless", "-auth", str(XAUTHORITY_FILE)]
    return retlist


def start_xwayland() -> None:
    """A fresh cookie, then the server in the background; it outlives the launcher."""
    XAUTHORITY_FILE.touch()
    XAUTHORITY_FILE.chmod(0o600)
    subprocess.run(["xauth", "-q", "-f", str(XAUTHORITY_FILE), "remove", X_DISPLAY], stderr=subprocess.DEVNULL)
    subprocess.run(
        ["xauth", "-q", "-f", str(XAUTHORITY_FILE), "add", X_DISPLAY, "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)],
        check=True,
    )
    subprocess.Popen(xwayland_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)


def cmd_session(args) -> int:
    if not args.dry_run and not common.linux_only("client session"):
        retval = 1
        return retval
    launcher = [str(GATEWAY_LAUNCHER), *without_separator(args.args)]
    if args.dry_run:
        print(f"would start unless running: {shlex.join(xwayland_command())}")
        print(f"would exec with XAUTHORITY={XAUTHORITY_FILE}: {shlex.join(launcher)}")
        retval = 0
        return retval
    me = pwd.getpwuid(os.geteuid()).pw_name
    if me != GW_USER:
        print(f"sandboxed-devenv: `client session` runs as {GW_USER}, not {me}; `client start` gets there",
              file=sys.stderr)
        retval = 1
        return retval
    if not xwayland_running():
        start_xwayland()
    os.environ["XAUTHORITY"] = str(XAUTHORITY_FILE)
    os.execv(launcher[0], launcher)


def cmd_exec(args) -> int:
    argv = without_separator(args.cmd)
    if not argv:
        print("sandboxed-devenv: `client exec` needs a command after `--`", file=sys.stderr)
        retval = 2
        return retval
    if not args.dry_run and not common.linux_only("client exec"):
        retval = 1
        return retval
    environment = session_environment(args)
    if environment is None:
        retval = 1
        return retval
    runtime_dir, sock, uid = environment
    command = sudo_command(sock, uid, argv, xauthority=True)
    if args.dry_run:
        print(f"would run: {shlex.join(command)}")
        retval = 0
        return retval
    os.execvp(command[0], command)
