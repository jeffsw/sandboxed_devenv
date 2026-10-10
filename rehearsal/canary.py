#!/usr/bin/env python3
"""canary: probe what a sandboxed principal can reach (ide_sandboxing_summary.md, section 6).

Run it as the principal under test, in the environment it really gets:
  client sandbox:  gw-exec canary --role client      (on desk, after gw-run in a GNOME session)
  backend VM:      canary --role backend              (as dev)
Each probe prints PASS, FAIL or INFO with a one-line detail. Exit status is 1 if any
probe failed. Standard library only; no root needed.

What PASS means per probe is given in the doc; in short: the real user's secrets are
unreadable, no agent offers keys, egress matches the allow and deny lists, X11 and
the real user's D-Bus are closed, Wayland is reachable only for the client role and
exposes no capture or clipboard-manager globals.
"""
import argparse
import glob
import re
import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile

RESULTS = []


def report(status, name, detail):
    RESULTS.append((status, name, detail))
    print(f"{status:<4} {name}: {detail}", flush=True)


def run(cmd, env=None, timeout=15):
    """Run a command, return (exit code, combined output); never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def unix_connect(path, timeout=3):
    """Connect to a Unix socket; return the socket or an error string."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return s
    except OSError as e:
        s.close()
        return e.strerror or str(e)


# --- where is the real user's home? -----------------------------------------

def wayland_socket_path():
    disp = os.environ.get("WAYLAND_DISPLAY", "")
    if not disp:
        return None
    if disp.startswith("/"):
        return disp
    rt = os.environ.get("XDG_RUNTIME_DIR", "")
    return os.path.join(rt, disp) if rt else None


def guess_host_user():
    """The user whose desktop this sandbox borrows: owner of the Wayland socket,
    else uid 1000 (the first user on Ubuntu)."""
    sock = wayland_socket_path()
    uid = None
    if sock and os.path.exists(sock):
        uid = os.stat(sock).st_uid
    if uid is None or uid == os.getuid():
        uid = 1000
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


# --- probes -------------------------------------------------------------------

def probe_secrets(host_home):
    if host_home is None:
        report("INFO", "real user's secrets", "skipped: a backend VM has no real user; pass --host-home to probe one")
        return
    for rel in (".ssh", ".config/gh", ".config/1Password", ".config/JetBrains"):
        path = os.path.join(host_home, rel)
        try:
            entries = os.listdir(path)
        except PermissionError:
            report("PASS", f"read {path}", "permission denied")
            continue
        except FileNotFoundError:
            report("PASS", f"read {path}", "absent")
            continue
        readable = []
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isfile(full):
                try:
                    with open(full, "rb") as f:
                        f.read(1)
                    readable.append(name)
                except OSError:
                    pass
        report("FAIL", f"read {path}",
               f"listable, {len(entries)} entries, {len(readable)} files readable"
               + (f" (e.g. {readable[0]})" if readable else ""))


def agent_candidates(host_home):
    cands = []
    env = os.environ.get("SSH_AUTH_SOCK")
    if env:
        cands.append(("SSH_AUTH_SOCK", env))
    rt = os.environ.get("XDG_RUNTIME_DIR", "")
    for pat in ("ssh-agent.*", "gnupg/S.gpg-agent*", "keyring/ssh", "*.sock", "openssh_agent"):
        for p in glob.glob(os.path.join(rt, pat)) if rt else []:
            cands.append(("runtime dir", p))
    for p in glob.glob("/tmp/ssh-*/agent.*"):
        cands.append(("/tmp", p))
    for p in (os.path.join(host_home, ".ssh/auth.sock"), os.path.join(host_home, ".1password/agent.sock"),
              os.path.join(os.path.expanduser("~"), ".1password/agent.sock")):
        if os.path.exists(p):
            cands.append(("1Password", p))
    seen, out = set(), []
    for label, p in cands:
        if p not in seen:
            seen.add(p)
            out.append((label, p))
    return out


SSH_AGENT_LIKE = re.compile(r"(openssh_agent|ssh-agent|keyring/ssh|gcr/ssh|S\.gpg-agent\.ssh|auth\.sock|agent\.sock|SSH_AUTH)")


def probe_agents(host_home):
    found_keys = False
    cands = [(l, p) for l, p in agent_candidates(host_home) if l == "SSH_AUTH_SOCK" or SSH_AGENT_LIKE.search(p)]
    if not cands:
        report("PASS", "agent sockets", "no SSH agent socket visible")
        return
    for label, path in cands:
        s = unix_connect(path)
        if isinstance(s, str):
            report("PASS", f"agent {path}", f"not connectable ({s})")
            continue
        s.close()
        env = dict(os.environ, SSH_AUTH_SOCK=path)
        rc, out = run(["ssh-add", "-l"], env=env)
        if rc == 0 and out and "no identities" not in out.lower():
            found_keys = True
            report("FAIL", f"agent {path}", f"lists {len(out.splitlines())} key(s) [{label}]")
        else:
            report("PASS", f"agent {path}", f"connectable but offers no keys ({out.splitlines()[0] if out else rc}) [{label}]")
    if found_keys:
        report("FAIL", "agent summary", "an agent offers keys to this principal")


def probe_ssh_config(role):
    """The backend's OpenSSH must never consult an agent: the IDE hands git an agent
    socket proxied to the client (summary 4.5)."""
    rc, out = run(["ssh", "-G", "github.com"])
    conf = dict(l.split(" ", 1) for l in out.splitlines() if " " in l)
    agent, fwd = conf.get("identityagent", "?"), conf.get("forwardagent", "?")
    if role == "backend":
        ok = agent == "none" and fwd == "no"
        report("PASS" if ok else "FAIL", "ssh client config", f"identityagent {agent}, forwardagent {fwd}" + ("" if ok else " (expected none and no)"))
    else:
        report("PASS" if fwd == "no" else "FAIL", "ssh client config", f"forwardagent {fwd}, identityagent {agent}")


def probe_github(role):
    rc, out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "-o", "StrictHostKeyChecking=accept-new", "-T", "git@github.com"], timeout=25)
    line = next((l for l in out.splitlines() if "authenticated" in l or "denied" in l), out.splitlines()[-1] if out else "")
    if "successfully authenticated" in out:
        who = line.split("!")[0].replace("Hi ", "")
        if role == "client":
            report("FAIL", "ssh to github.com", f"authenticated as {who}; the client should hold no GitHub key")
        else:
            report("INFO", "ssh to github.com", f"authenticated as {who} (expected: the deploy key's repository)")
    elif "Permission denied" in out:
        report("PASS" if role == "client" else "INFO", "ssh to github.com", "denied: no usable key")
    else:
        report("INFO", "ssh to github.com", f"no answer: {line or rc}")


def tcp_reach(host, port=443, timeout=5):
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return None, f"name does not resolve ({e.strerror})"
    ip = infos[0][4][0]
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return ip, "connected"
    except OSError as e:
        return ip, f"no connection to {ip} ({e.strerror or e})"


def probe_egress(allow, deny, deny_ip):
    for host in allow:
        ip, what = tcp_reach(host)
        report("PASS" if what == "connected" else "FAIL", f"egress allowed {host}:443", f"{what}" + (f" [{ip}]" if ip and what == "connected" else ""))
    for host in deny:
        ip, what = tcp_reach(host)
        if ip is None:
            report("PASS", f"egress denied {host}:443", what + " (names are not even resolved)")
        elif what == "connected":
            report("FAIL", f"egress denied {host}:443", f"connected to {ip}")
        else:
            report("PASS", f"egress denied {host}:443", f"resolves to {ip} but {what}")
    if deny and deny_ip:
        try:
            with socket.create_connection((deny_ip, 443), timeout=5):
                report("FAIL", f"egress denied by address {deny_ip}:443", "connected")
        except OSError as e:
            report("PASS", f"egress denied by address {deny_ip}:443", f"blocked ({e.strerror or e})")


def probe_x11():
    socks = glob.glob("/tmp/.X11-unix/X*")
    disp = os.environ.get("DISPLAY")
    if not socks:
        report("PASS", "X11", f"no X11 socket in /tmp/.X11-unix (DISPLAY={disp!r})")
        return
    for path in socks:
        try:
            if os.stat(path).st_uid == os.getuid():
                report("INFO", f"X11 {path}", "this principal's own X server (the private Xwayland that gw-run starts for the embedded browser)")
                continue
        except OSError:
            pass
        s = unix_connect(path)
        if isinstance(s, str):
            report("PASS", f"X11 {path}", f"not connectable ({s})")
            continue
        try:
            # Connection setup with no authorization: byte order 'l', protocol 11.0, empty auth.
            s.sendall(b"l\x00" + struct.pack("<HHHH", 11, 0, 0, 0) + b"\x00\x00")
            head = s.recv(8)
        except OSError as e:
            report("PASS", f"X11 {path}", f"handshake failed ({e})")
            s.close()
            continue
        s.close()
        if head and head[0] == 1:
            report("FAIL", f"X11 {path}", "server accepted an unauthenticated connection; any X client can log keystrokes")
        else:
            report("PASS", f"X11 {path}", "socket open but the server refused the connection (no authorization)")


RISKY_WAYLAND = {
    "zwlr_screencopy_manager_v1": "screen capture",
    "zwlr_export_dmabuf_manager_v1": "screen capture",
    "ext_image_copy_capture_manager_v1": "screen capture",
    "zwlr_data_control_manager_v1": "clipboard without focus",
    "ext_data_control_manager_v1": "clipboard without focus",
    "zwp_virtual_keyboard_manager_v1": "input injection",
    "zwlr_virtual_pointer_manager_v1": "input injection",
    "org_kde_kwin_fake_input": "input injection",
    "zwlr_foreign_toplevel_manager_v1": "window control",
    "zwlr_layer_shell_v1": "overlay surfaces",
    "zwp_input_method_manager_v2": "keystroke access",
}


def wayland_globals(path):
    s = unix_connect(path)
    if isinstance(s, str):
        return None, s
    try:
        # wl_display@1.get_registry(new_id=2); wl_display@1.sync(new_id=3)
        s.sendall(struct.pack("<IIi", 1, (12 << 16) | 1, 2) + struct.pack("<IIi", 1, (12 << 16) | 0, 3))
        buf = b""
        names = []
        s.settimeout(5)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                obj, so = struct.unpack_from("<II", buf, 0)
                size, opcode = so >> 16, so & 0xFFFF
                if len(buf) < size:
                    break
                body, buf = buf[8:size], buf[size:]
                if obj == 2 and opcode == 0:  # wl_registry.global(name, interface, version)
                    _name, slen = struct.unpack_from("<II", body, 0)
                    iface = body[8:8 + slen - 1].decode(errors="replace")
                    names.append(iface)
                elif obj == 3 and opcode == 0:  # wl_callback.done
                    return names, None
                elif obj == 1 and opcode == 0:  # wl_display.error
                    return names, "display error"
        return names, "connection closed"
    except OSError as e:
        return None, str(e)
    finally:
        s.close()


def probe_wayland(role):
    path = wayland_socket_path()
    if not path or not os.path.exists(path):
        if role == "client":
            report("FAIL", "wayland", f"no socket (WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')!r}); the client cannot draw")
        else:
            report("PASS", "wayland", "no socket, as a backend should have")
        return
    names, err = wayland_globals(path)
    if names is None:
        report("FAIL" if role == "client" else "PASS", "wayland", f"{path}: not usable ({err})")
        return
    risky = sorted(n for n in names if n in RISKY_WAYLAND)
    if role != "client":
        report("FAIL", "wayland", f"{path} answers ({len(names)} globals); a backend should see no display")
    elif risky:
        report("FAIL", "wayland", f"{path} answers; risky globals: " + ", ".join(f"{n} ({RISKY_WAYLAND[n]})" for n in risky))
    else:
        report("PASS", "wayland", f"{path} answers with {len(names)} globals, none for capture, clipboard managers or input injection")
    if "wp_security_context_manager_v1" in names:
        report("INFO", "wayland security context", "compositor offers wp_security_context_manager_v1 (usable by a Flatpak-style sandbox)")


def dbus_hello_message():
    """A METHOD_CALL to org.freedesktop.DBus.Hello, little-endian, serial 1."""
    def string_field(code, sig, value):
        f = bytes([code, len(sig)]) + sig.encode() + b"\0"
        f += b"\0" * ((-len(f)) % 4)
        return f + struct.pack("<I", len(value)) + value.encode() + b"\0"
    fields = b""
    for code, sig, value in ((1, "o", "/org/freedesktop/DBus"), (6, "s", "org.freedesktop.DBus"),
                             (2, "s", "org.freedesktop.DBus"), (3, "s", "Hello")):
        fields += b"\0" * ((-len(fields)) % 8) + string_field(code, sig, value)
    msg = b"l" + bytes([1, 0, 1]) + struct.pack("<II", 0, 1) + struct.pack("<I", len(fields)) + fields
    return msg + b"\0" * ((-len(msg)) % 8)


def dbus_reply_kind(data):
    """METHOD_RETURN, ERROR <name>, or a description of anything else."""
    if len(data) < 16 or data[0] != ord("l"):
        return "no reply"
    kind = data[1]
    if kind == 2:
        return "METHOD_RETURN"
    if kind != 3:
        return f"message type {kind}"
    flen = struct.unpack_from("<I", data, 12)[0]
    off, end = 16, 16 + flen
    while off < end:
        off += (-off) % 8
        code, slen = data[off], data[off + 1]
        sig = data[off + 2:off + 2 + slen].decode()
        off += 2 + slen + 1
        if sig in ("s", "o"):
            off += (-off) % 4
            n = struct.unpack_from("<I", data, off)[0]
            val = data[off + 4:off + 4 + n].decode(errors="replace")
            off += 4 + n + 1
        elif sig == "g":
            n = data[off]
            val = data[off + 1:off + 1 + n].decode(errors="replace")
            off += 1 + n + 1
        elif sig == "u":
            off += (-off) % 4
            val = struct.unpack_from("<I", data, off)[0]
            off += 4
        else:
            break
        if code == 4:
            return f"ERROR {val}"
    return "ERROR"


def dbus_usable(path):
    """Returns (usable, detail). Authenticates with EXTERNAL, then sends Hello,
    because dbus-daemon answers OK to a foreign uid and only then goes silent."""
    s = unix_connect(path)
    if isinstance(s, str):
        return False, f"not connectable ({s})"
    try:
        s.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        s.settimeout(5)
        ans = s.recv(256).decode(errors="replace").strip()
        if not ans.startswith("OK"):
            return False, f"refused at authentication ({ans.split()[0] if ans else 'no answer'})"
        s.sendall(b"BEGIN\r\n" + dbus_hello_message())
        s.settimeout(5)
        try:
            kind = dbus_reply_kind(s.recv(4096))
        except socket.timeout:
            kind = "no reply to Hello"
    except OSError as e:
        return False, f"handshake failed ({e})"
    finally:
        s.close()
    if kind == "METHOD_RETURN":
        return True, "authenticated and Hello answered"
    return False, f"authenticated but then {kind}"


def probe_dbus(host_uid):
    if host_uid is not None:
        usable, detail = dbus_usable(f"/run/user/{host_uid}/bus")
        report("FAIL" if usable else "PASS", "dbus session (real user)", f"/run/user/{host_uid}/bus: {detail}")
    own = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    own_path = own.split("unix:path=")[-1].split(",")[0] if "unix:path=" in own else f"/run/user/{os.getuid()}/bus"
    usable, detail = dbus_usable(own_path)
    report("INFO", "dbus session (own)", f"{own_path}: {'usable' if usable else detail}")
    usable, detail = dbus_usable("/run/dbus/system_bus_socket")
    report("INFO", "dbus system", "usable (normal for any user; what it allows is policy)" if usable else detail)


KNOWN_RUNTIME_SOCKETS = (
    "wayland-0", "wayland-1", "bus", "pipewire-0", "pipewire-0-manager", "pulse/native",
    "keyring/control", "keyring/ssh", "gcr/ssh", "gnupg/S.gpg-agent", "gnupg/S.gpg-agent.ssh",
    "openssh_agent", "at-spi/bus", "systemd/io.systemd.Manager", "systemd/private",
    "speech-dispatcher/speechd.sock", "snapd-session-agent.socket", "pk-debconf-socket",
    "docker.sock", "podman/podman.sock", "1password/agent.sock", "doc", "gvfs",
)


def probe_host_runtime(host_uid):
    """Everything in the real user's runtime directory other than the Wayland socket
    should be closed to this principal. Known names are tried because the directory
    itself is usually not listable."""
    rt = f"/run/user/{host_uid}"
    if not os.path.isdir(rt) and not os.path.exists(rt):
        report("PASS", "real user's runtime dir", f"{rt} absent")
        return
    names = list(KNOWN_RUNTIME_SOCKETS)
    try:
        listing = os.listdir(rt)
        names += [n for n in listing if n not in names]
        report("INFO", "real user's runtime dir", f"{rt} is listable ({len(listing)} entries)")
    except PermissionError:
        report("INFO", "real user's runtime dir", f"{rt} not listable; probing {len(names)} known names")
    reachable, blocked = [], 0
    for rel in names:
        path = os.path.join(rt, rel)
        try:
            st = os.stat(path)
        except PermissionError:
            blocked += 1
            continue
        except OSError:
            continue
        import stat as _stat
        if _stat.S_ISDIR(st.st_mode):
            if os.access(path, os.X_OK):
                reachable.append(rel + "/")
            else:
                blocked += 1
            continue
        if not _stat.S_ISSOCK(st.st_mode):
            continue
        s = unix_connect(path)
        if isinstance(s, str):
            blocked += 1
        else:
            s.close()
            reachable.append(rel)
    wayland = [r for r in reachable if r.startswith("wayland-")]
    others = [r for r in reachable if r not in wayland]
    if others:
        report("FAIL", "real user's runtime dir", "reachable besides Wayland: " + ", ".join(others)
               + " (connect or traverse succeeds; protocol-level checks not probed except D-Bus)")
    else:
        report("PASS", "real user's runtime dir", f"only {', '.join(wayland) or 'nothing'} reachable; {blocked} other entries closed")


def probe_dri():
    nodes = sorted(glob.glob("/dev/dri/*"))
    if not nodes:
        report("PASS", "/dev/dri", "no device nodes")
        return
    opened = []
    for n in nodes:
        if os.path.isdir(n):
            continue
        try:
            os.close(os.open(n, os.O_RDWR))
            opened.append(os.path.basename(n))
        except OSError:
            pass
    if opened:
        report("INFO", "/dev/dri", "openable: " + ", ".join(opened) + " (GPU reachable; not needed by the client)")
    else:
        report("PASS", "/dev/dri", f"{len(nodes)} node(s) present, none openable")


def probe_clipboard():
    if not shutil.which("wl-paste"):
        report("INFO", "clipboard", "wl-paste not installed; skipped")
        return
    if not wayland_socket_path():
        report("INFO", "clipboard", "no Wayland socket; skipped")
        return
    rc, out = run(["wl-paste", "--no-newline"], timeout=4)
    if rc == 0:
        report("INFO", "clipboard", f"wl-paste read {len(out)} bytes without focus being arranged (a Wayland client can read the selection when it has or briefly takes focus)")
    else:
        report("INFO", "clipboard", f"wl-paste got nothing (rc {rc}: {out.splitlines()[0] if out else ''})")


def probe_writes():
    home = os.path.expanduser("~")
    rcs = [f for f in (".bashrc", ".profile", ".zshrc") if os.path.exists(os.path.join(home, f))]
    writable = [f for f in rcs if os.access(os.path.join(home, f), os.W_OK)]
    report("INFO", "shell rc files", f"{', '.join(writable) or 'none'} writable of {', '.join(rcs) or 'none present'}")
    if not shutil.which("git"):
        report("INFO", "git hooks", "git not installed; skipped")
        return
    tmp = tempfile.mkdtemp(prefix="canary-", dir=home)
    try:
        rc, out = run(["git", "init", "-q", tmp])
        hook = os.path.join(tmp, ".git", "hooks", "pre-commit")
        try:
            with open(hook, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            report("INFO", "git hooks", f"writable in a repository under {home} (a sandbox that blocks hook writes would report otherwise)")
        except OSError as e:
            report("INFO", "git hooks", f"not writable ({e.strerror})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_identity():
    rc, kernel = run(["uname", "-r"])
    rc, virt = run(["systemd-detect-virt"])
    me = pwd.getpwuid(os.getuid()).pw_name
    report("INFO", "identity", f"{me} (uid {os.getuid()}) on {socket.gethostname()}, kernel {kernel}, virtualization: {virt or 'unknown'}")
    diffs, same, unknown = [], [], []
    for ns in ("pid", "net", "mnt", "user", "ipc", "uts"):
        try:
            mine = os.readlink(f"/proc/self/ns/{ns}")
            init = os.readlink(f"/proc/1/ns/{ns}")
            (diffs if mine != init else same).append(ns)
        except OSError:
            unknown.append(ns)
    if not diffs and not same:
        report("INFO", "namespaces", "cannot compare with PID 1 without root")
    else:
        report("INFO", "namespaces", f"differ from PID 1: {', '.join(diffs) or 'none'}; same: {', '.join(same) or 'none'}"
               + (f"; unreadable: {', '.join(unknown)}" if unknown else ""))


# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=("client", "backend"), required=True,
                    help="client: the sandbox that draws the IDE; backend: the VM that runs the project")
    ap.add_argument("--host-home", help="the real user's home (default: owner of the Wayland socket, else uid 1000)")
    ap.add_argument("--allow", action="append", default=[], metavar="HOST", help="host expected reachable on 443 (default github.com)")
    ap.add_argument("--deny", action="append", default=[], metavar="HOST", help="host expected unreachable on 443 (give one once egress control is on)")
    ap.add_argument("--deny-ip", default="1.1.1.1", help="address expected unreachable on 443 when --deny is used (default 1.1.1.1)")
    args = ap.parse_args()

    host = guess_host_user()
    if args.host_home:
        host_home = args.host_home
        host_uid = os.stat(host_home).st_uid if os.path.exists(host_home) else None
    elif args.role == "client" and host and host.pw_uid != os.getuid():
        host_home, host_uid = host.pw_dir, host.pw_uid
    else:
        host_home, host_uid = None, None
    print(f"canary: role {args.role}, running as {pwd.getpwuid(os.getuid()).pw_name}, "
          f"real user's home {host_home or 'none (not probed)'}")

    probe_secrets(host_home)
    probe_agents(host_home or "/nonexistent")
    probe_ssh_config(args.role)
    probe_github(args.role)
    probe_egress(args.allow or ["github.com"], args.deny, args.deny_ip)
    probe_x11()
    probe_wayland(args.role)
    if host_uid is not None:
        probe_host_runtime(host_uid)
    probe_dbus(host_uid)
    probe_dri()
    probe_clipboard()
    probe_writes()
    probe_identity()

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    passes = [r for r in RESULTS if r[0] == "PASS"]
    print(f"summary: {len(passes)} pass, {len(fails)} fail, {len(RESULTS) - len(passes) - len(fails)} info")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
