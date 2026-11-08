"""`sandboxed-devenv client-host ...`: the client host VM on the Mac rig (control role).

On the Linux laptop the client host is the laptop itself and `sudo sandboxed-devenv host install`
provisions it. On the Mac the client host is the `desk` VM, and `client-host new` is what the
rig's provision-desk.sh and the desk half of create-vms.sh did (summary 4.9): Ubuntu's cloud
image as a raw disk, a seed from the host cloud-init (the desktop packages, the host user with
the rig key) and the host network-config (a static address, routes to both halves of the VM
subnet through the Mac), a VM with a display and input devices, then over SSH once cloud-init is
done: the wheel and `sudo pipx install --global`, the sandbox key pair into the host user's
secrets directory (gw's key to the backends; `host install` copies it to
/home/gw/.ssh/vm_ed25519; the control key never goes to desk), `sudo sandboxed-devenv host
install`, and a reboot into GNOME. The same two commands provision the Linux laptop on its
install day. `client-host provision` repeats the SSH half on an existing desk: a newer wheel, a
new sandbox key, a changed policy.

The VM is `<vm_name_prefix>desk`, its address `utm.client_host_address` and its login the user in
`client_host` (config.yaml). The login password comes from the DESK_PASSWORD environment variable,
default `rehearsal`, as the rig had it.
"""

import argparse
import datetime
import os
import shutil
import sys
import time

from sandboxed_devenv import common, config, control_creds, control_driver, control_images, remote, render
from sandboxed_devenv.control_driver import DriverError, VmSpec
from sandboxed_devenv.remote import RemoteError
from sandboxed_devenv.render import RenderError

COMMAND = "client-host new"
KNOWN_HOSTS_NAME = "client-host"
BUILD_DIR_NAME = ".client-host-build"
# SSH comes up early; the desktop install that follows takes ten minutes or more.
SSH_TIMEOUT = 900
CLOUD_INIT_TIMEOUT = 3600
STOP_TIMEOUT = 180
FORCE_STOP_TIMEOUT = 60
ERRORS = (RemoteError, DriverError, RenderError, config.SecretError, OSError)


def register(subparsers):
    group = common.add_group(subparsers, "client-host", "the client host VM on the Mac rig: desk (control)")

    new = group.add_parser(
        "new",
        help="create desk from the cloud image and the host cloud-init, install the package, run `host install`, reboot",
    )
    new.add_argument("--wheel", metavar="PATH", help="push this wheel instead of building one from the checkout")
    new.add_argument("--cpus", metavar="N", type=int, help="CPUs for desk (default: the backend default)")
    new.add_argument("--memory-gib", metavar="N", type=int, help="memory for desk in GiB (default: the backend default)")
    new.add_argument("--replace", action="store_true", help="delete an existing desk first (its GNOME session and gw's Gateway state go with it)")
    new.set_defaults(handler=cmd_new)

    provision = group.add_parser(
        "provision",
        help="on an existing desk: push the wheel and `sudo pipx install --global`, the sandbox key, `sudo sandboxed-devenv host install`",
    )
    provision.add_argument("--wheel", metavar="PATH", help="push this wheel instead of building one from the checkout")
    provision.set_defaults(handler=cmd_provision)

    rm = group.add_parser("rm", help="stop and delete desk")
    rm.add_argument("--yes", action="store_true", help="delete without asking, for scripts")
    rm.set_defaults(handler=cmd_rm)

    ssh = group.add_parser("ssh", help="a shell on desk as its host user with the rig key")
    ssh.add_argument("cmd", metavar="CMD", nargs=argparse.REMAINDER,
                     help="a command to run there instead of a shell. No shell expansion.")
    ssh.set_defaults(handler=cmd_ssh)


def client_host_target(user_config: config.UserConfig) -> tuple[str, str, int]:
    """(user, host, port) of the client host VM, or RemoteError when this machine is the client host."""
    parsed = remote.parse_client_host(user_config.client_host) if user_config.client_host != "local" else None
    if parsed is None:
        raise RemoteError("client_host is `local` in config.yaml: this machine is the client host, and "
                          "`sudo sandboxed-devenv host install` provisions it; `client-host` commands are for the Mac rig")
    retval = parsed
    return retval


def client(user_config: config.UserConfig, runner: common.Runner) -> remote.Remote:
    user, host, port = client_host_target(user_config)
    retval = remote.Remote(user=user, host=host, key=remote.require_control_key(), known_hosts=remote.known_hosts_file(KNOWN_HOSTS_NAME),
                           port=port, runner=runner)
    return retval


def wheel_for(args, runner: common.Runner, build_dir):
    if args.wheel:
        retval = control_images.pathlib.Path(args.wheel)
        if not retval.is_file():
            raise RenderError(f"no wheel at {retval}")
    else:
        checkout = render.find_checkout()
        if checkout is None:
            raise RenderError("run this inside a checkout of the repository, or pass --wheel PATH")
        retval = render.build_wheel(checkout, build_dir, runner)
    return retval


def stop_and_delete(driver: control_driver.Driver, vm: str) -> None:
    state = driver.state(vm)
    if state != control_driver.STOPPED:
        driver.stop(vm, force=False)
        try:
            control_driver.wait_for_state(driver, vm, control_driver.STOPPED, STOP_TIMEOUT)
        except DriverError:
            driver.stop(vm, force=True)
            control_driver.wait_for_state(driver, vm, control_driver.STOPPED, FORCE_STOP_TIMEOUT)
    driver.delete(vm)


def install_and_configure(desk: remote.Remote, wheel, command: str) -> None:
    """The SSH half shared by `new` and `provision`: the package, the sandbox key pair into the
    host user's secrets directory (a legacy gw_ed25519 there removed, since it is the control key
    now), then `sudo sandboxed-devenv host install`."""
    with common.slow_step(command, "package", f"pushing {wheel.name} and running sudo pipx install --global"):
        version = desk.install_wheel(wheel)
    print(f"{command}: package: {version}", flush=True)
    home = (desk.run(["printenv", "HOME"], capture=True).stdout or "").strip() or f"/home/{desk.user}"
    remote_secrets = f"{home}/{config.HOME_DIR_NAME}/secrets"
    key = remote.sandbox_key_path()
    desk.run(["mkdir", "-p", remote_secrets])
    desk.run(["chmod", "700", f"{home}/{config.HOME_DIR_NAME}", remote_secrets])
    desk.push([key, key.with_suffix(".pub")], f"{remote_secrets}/")
    desk.run(["chmod", "600", f"{remote_secrets}/{config.SANDBOX_KEY_NAME}"])
    desk.run(["rm", "-f", f"{remote_secrets}/{config.LEGACY_RIG_KEY_NAME}", f"{remote_secrets}/{config.LEGACY_RIG_KEY_NAME}.pub"])
    with common.slow_step(command, "host install", "sudo sandboxed-devenv host install: the gw user, the files, AppArmor, "
                          "nftables, the Gateway launcher (several hundred MB on a fresh desk); minutes"):
        desk.tool(["host", "install"], sudo=True)


def provision(desk: remote.Remote, wheel) -> None:
    """Everything that happens inside the booted desk: wait for SSH and for cloud-init, then
    the shared SSH half, then a reboot into GNOME."""
    with common.slow_step(COMMAND, "first boot", f"waiting for SSH as {desk.target}; the users module runs early in boot"):
        desk.wait_for_ssh(SSH_TIMEOUT)
    with common.slow_step(COMMAND, "cloud-init", "waiting for the desktop install (ubuntu-desktop-minimal and the host "
                          "packages); ten minutes or more"):
        desk.wait_for_cloud_init(CLOUD_INIT_TIMEOUT)
    install_and_configure(desk, wheel, COMMAND)
    desk.run(["sudo", "systemctl", "reboot"], check=False, quiet=True)


def new(args) -> int:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    versions = config.load_versions()
    architecture = common.architecture()
    driver = control_driver.load_driver(runner)
    user, host, port = client_host_target(user_config)
    if host != user_config.utm.client_host_address:
        raise RemoteError(f"client_host names {host} but utm.client_host_address is {user_config.utm.client_host_address}; "
                          "make them agree in config.yaml")
    vm = user_config.client_host_vm_name()
    cpus = args.cpus or user_config.backend.cpus
    memory_gib = args.memory_gib or user_config.backend.memory_gib
    disk_gib = user_config.backend.disk_gib
    exists = driver.exists(vm)
    if exists and not args.replace:
        raise DriverError(f"{vm} exists ({driver.state(vm)}); `client-host new --replace` deletes it first, or "
                          f"`client-host rm`")
    keys = control_creds.seed_key_values(args.dry_run, runner)
    values = render.default_values(user_config, CONTROL_PUBKEY=keys["CONTROL_PUBKEY"])
    values.update(render.HOST_KEY_DIGEST_VALUES)
    network_config = render.cloudinit_document("host", "cloudinit", "network-config", values)
    cloud_image = control_images.cloud_image_path(versions, architecture)
    if args.dry_run:
        if exists:
            print(f"would stop and delete the existing {vm}")
        print(f"would fetch {cloud_image.name} into {cloud_image.parent} unless present, checked against SHA256SUMS")
        print(f"would convert it to a raw disk of {disk_gib} GiB (sparse) with qemu-img")
        print(f"would render the host seed (hostname {config.CLIENT_HOST_HOSTNAME}, user {user}, address {host}, routes to "
              f"{values['SUBNET_LOWER_HALF']} and {values['SUBNET_UPPER_HALF']} via {values['GATEWAY']}) and create VM {vm} "
              f"with {cpus} CPUs, {memory_gib} GiB, a display and input devices")
        print(f"would start {vm}, wait for SSH as {user}@{host}, then for cloud-init (the desktop install)")
        print(f"would push {args.wheel or 'the wheel built from the checkout'}, run `sudo pipx install --global`, place the "
              f"sandbox key pair in ~{user}/{config.HOME_DIR_NAME}/secrets/, run `sudo sandboxed-devenv host install`, reboot")
        print("would mint desk's SSH host key into the seed and pin it; desk's host user authorizes the control key only")
        retval = 0
        return retval
    started = time.monotonic()
    build_dir = config.images_dir() / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    try:
        wheel = wheel_for(args, runner, build_dir)
        cloud_image = control_images.ensure_cloud_image(versions, architecture)
        disk = build_dir / f"{vm}.img"
        with common.slow_step(COMMAND, "raw disk", f"converting {cloud_image.name} to {disk} and growing it to {disk_gib} GiB "
                              "(sparse: the filesystem fills it at first boot)"):
            control_images.prepare_disk(cloud_image, disk, disk_gib, runner)
        seed_dir = build_dir / "seed"
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
        host_private, host_public = remote.generate_host_key(config.CLIENT_HOST_HOSTNAME)
        values.update(render.host_key_values(host_private, host_public))
        del host_private
        user_data = render.cloudinit_document("host", "cloudinit", "user-data", values)
        render.write_seed(seed_dir, user_data, f"{config.CLIENT_HOST_HOSTNAME}-{stamp}", config.CLIENT_HOST_HOSTNAME,
                          network_config)
        if exists:
            print(f"{COMMAND}: stopping and deleting the existing {vm}", flush=True)
            stop_and_delete(driver, vm)
        remote.forget_host_keys(KNOWN_HOSTS_NAME)
        remote.pin_host_key(KNOWN_HOSTS_NAME, host, host_public)
        with common.slow_step(COMMAND, "create", f"UTM imports the {disk_gib} GiB disk and the seed ISO into the bundle of {vm}, "
                              "then relaunches to pick up the input devices"):
            driver.create(VmSpec(name=vm, disk=disk, cpus=cpus, memory_gib=memory_gib, seed_dir=seed_dir, display=True,
                                 disk_gib=disk_gib))
        disk.unlink()
        driver.start(vm)
        desk = client(user_config, runner)
        try:
            provision(desk, wheel)
        except (RemoteError, DriverError) as error:
            print(f"{COMMAND}: failed: {error}", file=sys.stderr)
            print(f"{COMMAND}: {vm} is left as it is for inspection: `utmctl status {vm}`, `sandboxed-devenv client-host ssh`, "
                  "or its window in UTM; `client-host new --replace` starts over", file=sys.stderr)
            retval = 1
            return retval
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    print(f"{COMMAND}: {vm} provisioned at {host} and rebooting into GNOME; total {common.format_duration(time.monotonic() - started)}")
    print(f"{COMMAND}: log in at its UTM window as {user} (password from DESK_PASSWORD, default {render.DEFAULT_DESK_PASSWORD}) "
          "and run `sandboxed-devenv client start`; `sandboxed-devenv new PROJECT` then adds backends it can reach")
    retval = 0
    return retval


def cmd_new(args) -> int:
    try:
        retval = new(args)
    except ERRORS as error:
        retval = remote.report(error, COMMAND)
    return retval


def provision_existing(args) -> int:
    command = "client-host provision"
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    user, host, port = client_host_target(user_config)
    if args.dry_run:
        print(f"would push {args.wheel or 'the wheel built from the checkout'} to {user}@{host}, run `sudo pipx install "
              f"--global`, place the sandbox key pair in ~{user}/{config.HOME_DIR_NAME}/secrets/ (removing a legacy "
              f"{config.LEGACY_RIG_KEY_NAME}), and run `sudo sandboxed-devenv host install`")
        retval = 0
        return retval
    control_creds.ensure_keys(runner)
    started = time.monotonic()
    build_dir = config.images_dir() / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    try:
        wheel = wheel_for(args, runner, build_dir)
        desk = client(user_config, runner)
        install_and_configure(desk, wheel, command)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    print(f"{command}: {user}@{host} provisioned; total {common.format_duration(time.monotonic() - started)}")
    retval = 0
    return retval


def cmd_provision(args) -> int:
    try:
        retval = provision_existing(args)
    except ERRORS as error:
        retval = remote.report(error, "client-host provision")
    return retval


def rm(args) -> int:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    driver = control_driver.load_driver(runner)
    vm = user_config.client_host_vm_name()
    if not driver.exists(vm):
        print(f"client-host rm: no VM named {vm}")
        retval = 0
        return retval
    state = driver.state(vm)
    if args.dry_run:
        print(f"would stop {vm} ({state}) if needed and delete it")
        retval = 0
        return retval
    if not args.yes:
        if not sys.stdin.isatty():
            print(f"client-host rm: refusing to delete {vm} without --yes, since there is no terminal to ask", file=sys.stderr)
            retval = 1
            return retval
        answer = input(f"client-host rm: delete {vm} ({state}), its disk, gw's Gateway state and all? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            retval = 1
            return retval
    stop_and_delete(driver, vm)
    remote.forget_host_keys(KNOWN_HOSTS_NAME)
    print(f"client-host rm: {vm} deleted")
    retval = 0
    return retval


def cmd_rm(args) -> int:
    try:
        retval = rm(args)
    except ERRORS as error:
        retval = remote.report(error, "client-host rm")
    return retval


def ssh(args) -> int:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    desk = client(user_config, runner)
    remote_command = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else list(args.cmd)
    command = desk.ssh_command(remote_command or None, tty=not remote_command)
    if args.dry_run:
        print(f"would run: {' '.join(command)}")
        retval = 0
        return retval
    os.execvp(command[0], command)


def cmd_ssh(args) -> int:
    try:
        retval = ssh(args)
    except ERRORS as error:
        retval = remote.report(error, "client-host ssh")
    return retval
