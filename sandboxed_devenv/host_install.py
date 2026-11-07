"""`sandboxed-devenv host ...`: provision and check the client host (client host role).

`host install` replaces the write_files and runcmd of the desk cloud-init as it was before M1,
so a fresh desk and the Linux laptop are provisioned by the same two commands once the wheel
is on the machine: `sudo pipx install --global WHEEL` and `sudo sandboxed-devenv host install`.
Every step is idempotent and reports changed, unchanged, would change or failed. `--check`
applies nothing and exits 1 when anything would change (drift checks on the laptop, the
headless harness); `--dry-run` applies nothing either and exits 0, and without root it prints
the plan instead of inspecting the host.

`host check` runs the canary in the client role as gw, through the same `sudo env` as
`client exec`, and exits with the canary's status.
"""

import dataclasses
import grp
import os
import pathlib
import platform
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from sandboxed_devenv import common, config, host_client, render

GW_USER = host_client.GW_USER
GW_HOME = host_client.GW_HOME
GW_KEY = GW_HOME / ".ssh" / "vm_ed25519"
GATEWAY_DIR = host_client.GATEWAY_DIR
# Written after unpacking, so the installed version is known whatever the tarball's own layout.
GATEWAY_VERSION_FILE = GATEWAY_DIR / ".sandboxed-devenv-version"
GATEWAY_VMOPTIONS = GATEWAY_DIR / "bin" / "gateway64.vmoptions"
# Gateway 2026.2.2 ships no toolkit option; without this line it starts on X11 (8 October 2026).
TOOLKIT_OPTION = "-Dawt.toolkit.name=auto"
APPARMOR_PROFILE = pathlib.Path("/etc/apparmor.d/jetbrains-client")
APPARMOR_PROFILE_NAME = "jetbrains-client"
APPARMOR_LOADED = pathlib.Path("/sys/kernel/security/apparmor/profiles")
SYSCTL_FILE = pathlib.Path("/etc/sysctl.d/99-vmrouter.conf")
IP_FORWARD = pathlib.Path("/proc/sys/net/ipv4/ip_forward")
NFTABLES_CONF = pathlib.Path("/etc/nftables.conf")
LINGER_DIR = pathlib.Path("/var/lib/systemd/linger")
# What the client needs beyond a desktop: ACLs, the allowlist, clipboard, the private X server
# and its cookie. The desk cloud-init installs the same list so its first boot is one apt run.
HOST_PACKAGES = ["acl", "nftables", "wl-clipboard", "xwayland-run", "xauth"]

CHANGED = "changed"
UNCHANGED = "unchanged"
WOULD_CHANGE = "would change"
FAILED = "failed"


@dataclasses.dataclass
class Context:
    apply: bool
    values: dict[str, str]
    secrets_dir: pathlib.Path
    changed: set = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class Outcome:
    label: str
    status: str
    detail: str = ""

    def line(self) -> str:
        retval = f"host install: {self.label}: {self.status}"
        if self.detail:
            retval += f" ({self.detail})"
        return retval


def register(subparsers):
    host = common.add_group(subparsers, "host", "provision and check the client host; Linux only (client host)")

    install = host.add_parser(
        "install",
        help="the gw user and linger, files from the manifest, AppArmor, sysctl, nftables, the Gateway tarball; needs root",
    )
    install.add_argument("--check", action="store_true", help="apply nothing; exit 1 if anything would change (drift check)")
    install.set_defaults(handler=cmd_install)

    check = host.add_parser("check", help="run the canary in the client role as gw and print the summary")
    check.set_defaults(handler=cmd_check)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    retval = subprocess.run(command, text=True, **kwargs)
    return retval


def outcome(ctx: Context, label: str, detail: str, differs: bool, action, unchanged_detail: str = "") -> Outcome:
    """The shape of every step: nothing differs, unchanged; differs without apply, would change; with
    apply, run the action and report changed, or failed with the error."""
    if not differs:
        retval = Outcome(label, UNCHANGED, unchanged_detail)
    elif not ctx.apply:
        retval = Outcome(label, WOULD_CHANGE, detail)
    else:
        try:
            action()
            retval = Outcome(label, CHANGED, detail)
        except (OSError, subprocess.CalledProcessError) as error:
            retval = Outcome(label, FAILED, f"{detail}: {error}")
    return retval


def read_text(path: pathlib.Path) -> str | None:
    retval = None
    try:
        retval = path.read_text()
    except OSError:
        pass
    return retval


# --- steps ----------------------------------------------------------------------------------


def dpkg_installed(name: str) -> bool:
    completed = run(["dpkg-query", "--show", "--showformat=${Status}", name], capture_output=True)
    retval = completed.returncode == 0 and "install ok installed" in completed.stdout
    return retval


def apt_install(names: list[str]) -> None:
    environment = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    print(f"host install: packages: apt-get install {' '.join(names)}, which takes a moment", flush=True)
    run(["apt-get", "install", "--yes", "--no-install-recommends", *names], env=environment, check=True)


def step_packages(ctx: Context) -> list[Outcome]:
    missing = []
    for name in HOST_PACKAGES:
        if not dpkg_installed(name):
            missing.append(name)
    retlist = [
        outcome(ctx, "packages", f"apt-get install {' '.join(missing)}", bool(missing), lambda: apt_install(missing),
                f"{', '.join(HOST_PACKAGES)} installed"),
    ]
    return retlist


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        retval = True
    except KeyError:
        retval = False
    return retval


def useradd() -> None:
    # No password (useradd leaves it locked), no sudo, no groups: gw sees the Wayland socket only
    # while `client start` has lent it, and its own home.
    run(["useradd", "--create-home", "--home-dir", str(GW_HOME), "--shell", "/bin/bash",
         "--comment", "Gateway client sandbox", GW_USER], check=True)


def step_user(ctx: Context) -> list[Outcome]:
    exists = user_exists(GW_USER)
    unchanged = f"uid {pwd.getpwnam(GW_USER).pw_uid}" if exists else ""
    retlist = [outcome(ctx, f"user {GW_USER}", "useradd: the client sandbox user, no sudo, locked password",
                       not exists, useradd, unchanged)]
    if GW_HOME.is_dir():
        mode = stat.S_IMODE(GW_HOME.stat().st_mode)
        retlist.append(outcome(ctx, f"{GW_HOME} mode", f"chmod 0700 (was {mode:04o})", mode != 0o700,
                               lambda: GW_HOME.chmod(0o700), "0700"))
    else:
        retlist.append(Outcome(f"{GW_HOME} mode", WOULD_CHANGE if not ctx.apply else FAILED,
                               "created by useradd" if not ctx.apply else "no home after useradd"))
    return retlist


def step_linger(ctx: Context) -> list[Outcome]:
    marker = LINGER_DIR / GW_USER
    retlist = [outcome(ctx, "linger", f"loginctl enable-linger {GW_USER}: a user service manager and "
                       f"/run/user/<uid> at boot without a login", not marker.exists(),
                       lambda: run(["loginctl", "enable-linger", GW_USER], check=True), "enabled")]
    return retlist


def ensure_parents(dest: pathlib.Path, uid: int, gid: int) -> None:
    """Missing parent directories, owned like the file; `.ssh` closed, the rest world-readable."""
    missing = []
    parent = dest.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        os.chown(directory, uid, gid)
        directory.chmod(0o700 if directory.name == ".ssh" else 0o755)


def sync_file(dest: pathlib.Path, content: bytes, owner: str, group: str, mode: int, apply: bool) -> Outcome:
    """Content, owner, group and mode as given, written atomically; reports what differed."""
    label = f"file {dest}"
    try:
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as error:
        if apply:
            retval = Outcome(label, FAILED, f"no such user or group: {error}")
        else:
            retval = Outcome(label, WOULD_CHANGE, f"created once {owner}:{group} exist")
        return retval
    differences = []
    if not dest.exists():
        differences.append("created")
    else:
        status = dest.stat()
        if dest.read_bytes() != content:
            differences.append("content")
        if (status.st_uid, status.st_gid) != (uid, gid):
            differences.append("owner")
        if stat.S_IMODE(status.st_mode) != mode:
            differences.append("mode")
    if not differences:
        retval = Outcome(label, UNCHANGED, f"{owner}:{group} {mode:04o}")
        return retval
    detail = ", ".join(differences)
    if not apply:
        retval = Outcome(label, WOULD_CHANGE, detail)
        return retval
    try:
        ensure_parents(dest, uid, gid)
        temporary = dest.with_name(f".{dest.name}.sandboxed-devenv.tmp")
        temporary.write_bytes(content)
        os.chown(temporary, uid, gid)
        temporary.chmod(mode)
        os.replace(temporary, dest)
        retval = Outcome(label, CHANGED, detail)
    except OSError as error:
        retval = Outcome(label, FAILED, f"{detail}: {error}")
    return retval


def step_files(ctx: Context) -> list[Outcome]:
    retlist = []
    for name, entry in render.load_manifest()["host"].items():
        if entry.is_cloudinit:
            continue
        try:
            content = render.render_entry("host", name, entry, ctx.values)
            dest = render.rendered_dest(entry, ctx.values)
        except render.RenderError as error:
            retlist.append(Outcome(f"file {entry.dest}", FAILED, str(error)))
            continue
        result = sync_file(dest, content.encode(), entry.owner, entry.group, entry.mode_bits, ctx.apply)
        if result.status == CHANGED:
            ctx.changed.add(dest)
        retlist.append(result)
    return retlist


def step_key_pair(ctx: Context) -> list[Outcome]:
    private = ctx.secrets_dir / config.RIG_KEY_NAME
    public = ctx.secrets_dir / f"{config.RIG_KEY_NAME}.pub"
    if not (private.is_file() and public.is_file()):
        retlist = [Outcome("gw key pair", FAILED,
                           f"no {private} and {public.name}; the rig key pair belongs in {ctx.secrets_dir} (mode 0700)")]
        return retlist
    retlist = [
        sync_file(GW_KEY, private.read_bytes(), GW_USER, GW_USER, 0o600, ctx.apply),
        sync_file(GW_KEY.with_suffix(".pub"), public.read_bytes(), GW_USER, GW_USER, 0o644, ctx.apply),
    ]
    return retlist


def apparmor_loaded(name: str) -> bool:
    retval = False
    for line in (read_text(APPARMOR_LOADED) or "").splitlines():
        if line.split(" (")[0] == name:
            retval = True
            break
    return retval


def step_apparmor(ctx: Context) -> list[Outcome]:
    needed = APPARMOR_PROFILE in ctx.changed or not apparmor_loaded(APPARMOR_PROFILE_NAME) or not APPARMOR_PROFILE.exists()
    retlist = [outcome(ctx, "apparmor", f"apparmor_parser --replace {APPARMOR_PROFILE}", needed,
                       lambda: run(["apparmor_parser", "--replace", str(APPARMOR_PROFILE)], check=True),
                       f"{APPARMOR_PROFILE_NAME} profiles loaded")]
    return retlist


def step_sysctl(ctx: Context) -> list[Outcome]:
    forwarding = (read_text(IP_FORWARD) or "").strip() == "1"
    needed = SYSCTL_FILE in ctx.changed or not forwarding
    retlist = [outcome(ctx, "sysctl", "sysctl --system (net.ipv4.ip_forward = 1)", needed,
                       lambda: run(["sysctl", "--quiet", "--system"], check=True), "net.ipv4.ip_forward = 1")]
    return retlist


def step_nftables(ctx: Context) -> list[Outcome]:
    retlist = []
    if NFTABLES_CONF.exists():
        completed = run(["nft", "--check", "--file", str(NFTABLES_CONF)], capture_output=True)
        if completed.returncode == 0:
            retlist.append(Outcome("nftables rules", UNCHANGED, "nft --check ok"))
        else:
            retlist.append(Outcome("nftables rules", FAILED, completed.stderr.strip().splitlines()[0]))
    else:
        retlist.append(Outcome("nftables rules", WOULD_CHANGE if not ctx.apply else FAILED, f"no {NFTABLES_CONF}"))
    completed = run(["systemctl", "is-enabled", "nftables"], capture_output=True)
    enabled = completed.stdout.strip() == "enabled"
    retlist.append(outcome(ctx, "nftables service", "systemctl disable nftables: the allowlist stays off until policy enables it",
                           enabled, lambda: run(["systemctl", "disable", "--quiet", "nftables"], check=True),
                           "disabled until policy enables it"))
    return retlist


def gateway_url(version: str) -> str:
    if platform.machine() == "aarch64":
        retval = f"https://download.jetbrains.com/idea/gateway/JetBrainsGateway-{version}-aarch64.tar.gz"
    else:
        retval = f"https://download.jetbrains.com/idea/gateway/JetBrainsGateway-{version}.tar.gz"
    return retval


def install_gateway(version: str) -> None:
    url = gateway_url(version)
    print(f"host install: gateway: downloading {url} and unpacking it under {GATEWAY_DIR}; "
          "the launcher is several hundred MB, so this takes minutes", flush=True)
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(dir=GW_HOME, prefix=".gateway-", suffix=".tar.gz", delete=False) as archive:
        with urllib.request.urlopen(url, timeout=60) as response:
            shutil.copyfileobj(response, archive)
    try:
        if GATEWAY_DIR.exists():
            shutil.rmtree(GATEWAY_DIR)
        GATEWAY_DIR.mkdir()
        run(["tar", "--extract", "--gzip", "--strip-components=1", f"--directory={GATEWAY_DIR}", f"--file={archive.name}"],
            check=True)
    finally:
        os.unlink(archive.name)
    GATEWAY_VERSION_FILE.write_text(f"{version}\n")
    run(["chown", "--recursive", f"{GW_USER}:{GW_USER}", str(GATEWAY_DIR)], check=True)
    print(f"host install: gateway: unpacked in {time.monotonic() - started:.0f} s", flush=True)


def append_toolkit_option() -> None:
    with open(GATEWAY_VMOPTIONS, "a") as handle:
        handle.write(f"{TOOLKIT_OPTION}\n")


def step_gateway(ctx: Context) -> list[Outcome]:
    version = ctx.values["GATEWAY_VERSION"]
    installed = (read_text(GATEWAY_VERSION_FILE) or "").strip() or None
    detail = f"JetBrainsGateway-{version} to {GATEWAY_DIR}"
    if installed:
        detail += f", replacing {installed}"
    retlist = [outcome(ctx, "gateway", detail, installed != version, lambda: install_gateway(version),
                       f"JetBrainsGateway-{version} at {GATEWAY_DIR}")]
    if GATEWAY_VMOPTIONS.exists():
        present = TOOLKIT_OPTION in (read_text(GATEWAY_VMOPTIONS) or "").splitlines()
        retlist.append(outcome(ctx, "gateway vmoptions", f"append {TOOLKIT_OPTION} to {GATEWAY_VMOPTIONS}: without it Gateway starts on X11",
                               not present, append_toolkit_option, f"{TOOLKIT_OPTION} present"))
    elif not ctx.apply:
        retlist.append(Outcome("gateway vmoptions", WOULD_CHANGE, f"append {TOOLKIT_OPTION} once the launcher is unpacked"))
    else:
        retlist.append(Outcome("gateway vmoptions", FAILED, f"no {GATEWAY_VMOPTIONS} after unpacking"))
    return retlist


STEPS = [
    step_packages,
    step_user,
    step_linger,
    step_files,
    step_key_pair,
    step_apparmor,
    step_sysctl,
    step_nftables,
    step_gateway,
]


def plan_lines(values: dict[str, str], secrets_dir: pathlib.Path) -> list[str]:
    """What `host install` ensures, for a dry run that cannot inspect the host."""
    retlist = [
        f"packages: {', '.join(HOST_PACKAGES)} installed (apt-get)",
        f"user {GW_USER}: the client sandbox user, no sudo, locked password, {GW_HOME} mode 0700",
        f"linger: loginctl enable-linger {GW_USER}",
    ]
    for name, entry in render.load_manifest()["host"].items():
        if entry.is_cloudinit:
            continue
        dest = render.rendered_dest(entry, values)
        retlist.append(f"file {dest} ({entry.owner}:{entry.group} {entry.mode}): {entry.description}")
    retlist.extend([
        f"gw key pair: {GW_KEY} and .pub from {secrets_dir / config.RIG_KEY_NAME}",
        f"apparmor: apparmor_parser --replace {APPARMOR_PROFILE}",
        "sysctl: sysctl --system (net.ipv4.ip_forward = 1)",
        f"nftables: nft --check {NFTABLES_CONF}; the service stays disabled until policy enables it",
        f"gateway: JetBrainsGateway-{values['GATEWAY_VERSION']} unpacked under {GATEWAY_DIR}, "
        f"{TOOLKIT_OPTION} in {GATEWAY_VMOPTIONS}",
    ])
    return retlist


def cmd_install(args) -> int:
    values = render.default_values()
    secrets_dir = config.secrets_dir()
    is_root = os.geteuid() == 0
    if args.dry_run and not (is_root and platform.system() == "Linux"):
        print("host install: the plan (a dry run without root on a client host, so its state is not inspected):")
        for line in plan_lines(values, secrets_dir):
            print(f"  {line}")
        retval = 0
        return retval
    if not common.linux_only("host install"):
        retval = 1
        return retval
    if not is_root:
        print("sandboxed-devenv: `host install` needs root: sudo sandboxed-devenv host install", file=sys.stderr)
        retval = 1
        return retval
    ctx = Context(apply=not (args.dry_run or args.check), values=values, secrets_dir=secrets_dir)
    counts = {CHANGED: 0, UNCHANGED: 0, WOULD_CHANGE: 0, FAILED: 0}
    for step in STEPS:
        for result in step(ctx):
            counts[result.status] += 1
            print(result.line(), flush=True)
    mode = "check" if args.check else "dry run" if args.dry_run else "install"
    print(f"host install: {mode}: {counts[CHANGED]} changed, {counts[UNCHANGED]} unchanged, "
          f"{counts[WOULD_CHANGE]} would change, {counts[FAILED]} failed")
    if counts[FAILED]:
        retval = 1
    elif args.check and counts[WOULD_CHANGE]:
        retval = 1
    else:
        retval = 0
    return retval


def cmd_check(args) -> int:
    if not args.dry_run and not common.linux_only("host check"):
        retval = 1
        return retval
    environment = host_client.session_environment(args)
    if environment is None:
        retval = 1
        return retval
    runtime_dir, sock, uid = environment
    command = host_client.sudo_command(sock, uid, [common.command_path(), "canary", "--role", "client"], xauthority=True)
    if args.dry_run:
        print(f"would run: {shlex.join(command)}")
        retval = 0
        return retval
    print("host check: the canary as gw; its Wayland and runtime-dir probes expect `client start` "
          "to have lent the socket in this session", flush=True)
    completed = subprocess.run(command)
    retval = completed.returncode
    return retval
