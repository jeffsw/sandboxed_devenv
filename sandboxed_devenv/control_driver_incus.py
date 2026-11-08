"""The Incus driver: backends as Incus virtual machines on the Linux laptop (PLAN.md M5).

Written before the laptop arrived and first run on it; what is below is the plan of record,
and the things to confirm there are marked. The shape:

- A VM is made from a disk file or a base image: `image build` imports Ubuntu's cloud image
  (QCOW2 as downloaded; Incus converts) as a temporary image, makes the build VM from it,
  and `keep_image` publishes the stopped VM as the alias ``sandboxed-devenv/<name>``; `new`
  makes a backend from that alias with `incus init <alias> <vm> --vm`.
- The seed is a NoCloud ISO (``cidata``), built with genisoimage or xorrisofs and kept under
  ``~/.sandboxed_devenv/seeds/<vm>.iso`` for the VM's life, attached as a disk device; the
  guest's cloud-init needs no agent and no Incus datasource for it. The host key the control
  side minted is in the seed as on UTM. To confirm: that a disk device with an ISO source is
  a CD-ROM the guest's cloud-init finds.
- Addresses are static leases on the managed bridge (``ipv4.address`` on the instance's nic
  device), from a pool on the bridge's subnet, so the control side knows a backend's address
  before it boots and the guest just takes DHCP; the seed carries no network-config.
- A clone goes through a temporary published image: `incus publish` of the stopped source,
  `incus init` from it, then the new project's seed, lease and limits. `incus copy` was the
  first choice, but on the laptop (Incus 6.0.5, a `dir` pool on ext4 over LUKS) it fails in
  the `dd` it runs with direct I/O, and it would carry the source's static lease into the
  copy; publishing is the path `image build` already proved. A full copy either way.
- CPUs and memory change on a running VM (`limits.cpu`, `limits.memory`); the serial console
  is `incus console --type=console`.
- The storage pool is `dir` on ``incus.storage_dir`` (``host install`` creates it); the VM's
  root disk takes its size from the spec.
- Every call goes to the `incus` client's default remote, which is the local daemon on the
  laptop; a machine whose default remote is elsewhere never loads this driver (macOS loads UTM).
"""

import dataclasses
import ipaddress
import json
import pathlib
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time

from sandboxed_devenv import common, config, control_driver, control_inventory
from sandboxed_devenv.control_driver import DriverError, VmInfo, VmSpec

DRIVER_NAME = "incus"
INCUS = "incus"
IMAGE_ALIAS_PREFIX = "sandboxed-devenv/"
TEMPORARY_IMAGE_PREFIX = "sandboxed-devenv-import-"
SEED_DEVICE = "cidata"
NIC_DEVICE = "eth0"
ROOT_DEVICE = "root"
SEED_VOLUME_NAME = "cidata"
# Incus's words for a VM's state, onto the driver protocol's.
STATES = {
    "Running": control_driver.STARTED,
    "Stopped": control_driver.STOPPED,
    "Starting": control_driver.STARTING,
    "Stopping": control_driver.STOPPING,
    "Frozen": control_driver.PAUSED,
}
# Incus names architectures as uname does; the metadata of an imported image must agree.
ARCHITECTURES = {"aarch64": "aarch64", "x86_64": "x86_64"}
DEFAULT_POOL_OFFSET = 20


def seeds_dir() -> pathlib.Path:
    retval = config.home_dir() / "seeds"
    return retval


def seed_iso_path(name: str) -> pathlib.Path:
    retval = seeds_dir() / f"{name}.iso"
    return retval


def seed_iso_command(seed_dir: pathlib.Path, iso: pathlib.Path) -> list[str]:
    """An ISO 9660 image with Joliet and Rock Ridge names and the ``cidata`` label, with whichever
    of the usual tools Ubuntu has."""
    if shutil.which("genisoimage"):
        retlist = ["genisoimage", "-quiet", "-output", str(iso), "-volid", SEED_VOLUME_NAME, "-joliet", "-rock", str(seed_dir)]
    elif shutil.which("xorrisofs"):
        retlist = ["xorrisofs", "-quiet", "-output", str(iso), "-volid", SEED_VOLUME_NAME, "-joliet", "-rational-rock", str(seed_dir)]
    elif shutil.which("mkisofs"):
        retlist = ["mkisofs", "-quiet", "-output", str(iso), "-volid", SEED_VOLUME_NAME, "-joliet", "-rock", str(seed_dir)]
    else:
        raise DriverError("no ISO tool on the PATH; `sudo apt install genisoimage` (or xorriso) provides one")
    return retlist


def alias_for(name: str) -> str:
    retval = f"{IMAGE_ALIAS_PREFIX}{name}"
    return retval


def parse_list(text: str) -> list[dict]:
    """`incus list --format json` parsed; an empty answer is no instances."""
    retlist = []
    if text.strip():
        retlist = json.loads(text)
    return retlist


def state_of(instance: dict) -> str:
    retval = STATES.get(instance.get("status", ""), control_driver.UNKNOWN)
    return retval


def addresses_of(instance: dict) -> list[str]:
    """Global IPv4 addresses the instance reports, which needs the agent inside the guest."""
    retlist = []
    network = ((instance.get("state") or {}).get("network") or {})
    for interface, details in network.items():
        if interface == "lo":
            continue
        for item in details.get("addresses") or []:
            if item.get("family") == "inet" and item.get("scope") == "global":
                retlist.append(item["address"])
    return retlist


def memory_gib_of(limit: str | None) -> int | None:
    """``8GiB``, ``8192MiB`` or bytes as Incus writes ``limits.memory``, in GiB."""
    retval = None
    if limit:
        text = limit.strip()
        units = {"GiB": 1 << 30, "MiB": 1 << 20, "KiB": 1 << 10, "GB": 10**9, "MB": 10**6, "B": 1}
        for unit, factor in units.items():
            if text.endswith(unit):
                retval = int(float(text[: -len(unit)]) * factor) // (1 << 30)
                break
        else:
            if text.isdigit():
                retval = int(text) // (1 << 30)
    return retval


def write_metadata(path: pathlib.Path, architecture: str, description: str) -> None:
    """The metadata tarball `incus image import` wants beside a disk image."""
    metadata = {
        "architecture": ARCHITECTURES.get(architecture, architecture),
        "creation_date": int(time.time()),
        "properties": {"os": "Ubuntu", "description": description},
    }
    document = json.dumps(metadata)
    with tempfile.TemporaryDirectory(prefix="sandboxed-devenv-metadata-") as temporary:
        yaml_path = pathlib.Path(temporary) / "metadata.yaml"
        yaml_path.write_text(document)
        with tarfile.open(path, "w:gz") as archive:
            archive.add(yaml_path, arcname="metadata.yaml")


@dataclasses.dataclass
class IncusDriver:
    """The Incus CLI with ``--format json`` where it has one."""

    name: str = DRIVER_NAME
    runner: common.Runner = dataclasses.field(default_factory=common.Runner)
    user_config: config.UserConfig = dataclasses.field(default_factory=config.load_user_config)
    static_network = False
    raw_disk = False
    live_resources = True

    def incus(self, *args: str, capture: bool = False, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
        kwargs = {}
        if capture:
            kwargs["capture_output"] = True
        if input is not None:
            kwargs["input"] = input
        retval = self.runner.run([INCUS, *args], **kwargs)
        if check and retval.returncode != 0:
            detail = (retval.stderr or "").strip() if capture else ""
            raise DriverError(f"incus {' '.join(args)} failed with status {retval.returncode}" + (f": {detail}" if detail else ""))
        return retval

    def query(self, *args: str) -> list | dict | None:
        completed = self.incus(*args, "--format", "json", capture=True)
        text = (completed.stdout or "").strip()
        retval = json.loads(text) if text else None
        return retval

    def available(self) -> str | None:
        retval = None
        if platform.system() != "Linux":
            retval = "the Incus driver runs on Linux"
        elif not shutil.which(INCUS):
            retval = "incus is not on the PATH; the Linux laptop installs it (docs/linux-laptop-install.md)"
        else:
            completed = self.runner.run([INCUS, "info"], capture_output=True)
            if completed.returncode != 0:
                retval = (f"incus is installed but `incus info` fails ({(completed.stderr or '').strip()[-120:]}); is this "
                          "user in the incus-admin group, and has `incus admin init` run?")
        return retval

    # --- the network -----------------------------------------------------------------------------

    def network_subnet(self, user_config: config.UserConfig) -> str:
        """The managed bridge's IPv4 subnet: ``incus.subnet`` when set, else the bridge's own
        ``ipv4.address`` (``10.x.y.1/24``) without the host part."""
        configured = user_config.incus.subnet
        if configured:
            retval = configured
        else:
            completed = self.incus("network", "get", user_config.incus.network, "ipv4.address", capture=True)
            text = (completed.stdout or "").strip()
            if not text or text == "none":
                raise DriverError(f"the Incus network {user_config.incus.network} has no IPv4 address; set incus.subnet "
                                  "in config.yaml or give the bridge one")
            retval = str(ipaddress.ip_interface(text).network)
        return retval

    def address_pool(self, user_config: config.UserConfig) -> control_inventory.AddressPool:
        """The bridge's subnet, backends from the twentieth address (or ``address_pool_start``),
        the build VM one below, the gateway reserved."""
        network = ipaddress.ip_network(self.network_subnet(user_config))
        gateway = network.network_address + 1
        if user_config.incus.address_pool_start:
            start = ipaddress.ip_address(user_config.incus.address_pool_start)
        else:
            start = network.network_address + DEFAULT_POOL_OFFSET
        retval = control_inventory.AddressPool(subnet=str(network), start=str(start), build_address=str(start - 1),
                                              reserved={str(gateway), str(start - 1)})
        return retval

    # --- instances ---------------------------------------------------------------------------------

    def list(self) -> list[VmInfo]:
        retlist = []
        for instance in parse_list((self.incus("list", "--format", "json", capture=True).stdout or "")):
            retlist.append(VmInfo(name=instance["name"], state=state_of(instance)))
        return retlist

    def instance(self, name: str) -> dict | None:
        retval = None
        for instance in parse_list((self.incus("list", name, "--format", "json", capture=True).stdout or "")):
            if instance.get("name") == name:
                retval = instance
                break
        return retval

    def exists(self, name: str) -> bool:
        retval = self.instance(name) is not None
        return retval

    def state(self, name: str) -> str:
        instance = self.instance(name)
        if instance is None:
            raise DriverError(f"no instance named {name}")
        retval = state_of(instance)
        return retval

    def make_seed(self, name: str, seed_dir: pathlib.Path) -> pathlib.Path:
        """The seed ISO under the seeds directory, replacing an older one of the same VM."""
        retval = seed_iso_path(name)
        retval.parent.mkdir(parents=True, exist_ok=True)
        if retval.exists():
            retval.unlink()
        completed = self.runner.run(seed_iso_command(seed_dir, retval), capture_output=True)
        if completed.returncode != 0:
            raise DriverError(f"the ISO tool failed with status {completed.returncode}: {(completed.stderr or '').strip()}")
        return retval

    def import_disk(self, disk: pathlib.Path, alias: str) -> None:
        """A disk image (QCOW2 or raw) as a temporary Incus image with the metadata it wants."""
        with tempfile.TemporaryDirectory(prefix="sandboxed-devenv-import-") as temporary:
            metadata = pathlib.Path(temporary) / "metadata.tar.gz"
            write_metadata(metadata, common.architecture(), f"sandboxed-devenv import of {disk.name}")
            self.incus("image", "import", str(metadata), str(disk), "--alias", alias)

    def configure(self, name: str, spec: VmSpec) -> None:
        """The seed device, the static lease and the limits on an instance that exists."""
        iso = self.make_seed(name, spec.seed_dir)
        existing = self.instance(name) or {}
        devices = existing.get("devices") or {}
        if SEED_DEVICE in devices:
            self.incus("config", "device", "remove", name, SEED_DEVICE)
        self.incus("config", "device", "add", name, SEED_DEVICE, "disk", f"source={iso}")
        self.incus("config", "set", name, f"limits.cpu={spec.cpus}", f"limits.memory={spec.memory_gib}GiB")
        if spec.address:
            if NIC_DEVICE in devices:
                self.incus("config", "device", "set", name, NIC_DEVICE, f"ipv4.address={spec.address}")
            else:
                self.incus("config", "device", "override", name, NIC_DEVICE, f"ipv4.address={spec.address}")

    def create(self, spec: VmSpec) -> None:
        """`incus init` from the base image alias or from a disk file imported for the purpose,
        then the seed, the lease and the limits."""
        temporary_alias = None
        if spec.image is not None:
            alias = alias_for(spec.image)
        elif spec.disk is not None:
            if not spec.disk.is_file() and not self.runner.dry_run:
                raise DriverError(f"no disk at {spec.disk}")
            temporary_alias = f"{TEMPORARY_IMAGE_PREFIX}{spec.name}"
            self.import_disk(spec.disk, temporary_alias)
            alias = temporary_alias
        else:
            raise DriverError(f"{spec.name}: neither a disk file nor a base image to make the VM from")
        arguments = ["init", alias, spec.name, "--vm", "--storage", self.user_config.incus.pool,
                     "-c", f"limits.cpu={spec.cpus}", "-c", f"limits.memory={spec.memory_gib}GiB"]
        if spec.disk_gib:
            arguments += ["-d", f"{ROOT_DEVICE},size={spec.disk_gib}GiB"]
        try:
            self.incus(*arguments)
        finally:
            if temporary_alias:
                self.incus("image", "delete", temporary_alias, check=False)
        self.configure(spec.name, spec)

    def start(self, name: str) -> None:
        self.incus("start", name)

    def stop(self, name: str, force: bool = False) -> None:
        if force:
            self.incus("stop", name, "--force")
        else:
            self.incus("stop", name)

    def delete(self, name: str) -> None:
        self.incus("delete", name, "--force")
        iso = seed_iso_path(name)
        if iso.exists() and not self.runner.dry_run:
            iso.unlink()

    def clone(self, name: str, new_name: str) -> None:
        self.incus("copy", name, new_name)

    def clone_from(self, source: str, spec: VmSpec) -> None:
        """The stopped source published as a temporary image, a new instance made from it with
        the spec's resources, then the new project's seed and lease; the temporary image goes."""
        alias = f"{TEMPORARY_IMAGE_PREFIX}clone-{spec.name}"
        self.incus("publish", source, "--alias", alias, "--reuse")
        try:
            arguments = ["init", alias, spec.name, "--vm", "--storage", self.user_config.incus.pool,
                         "-c", f"limits.cpu={spec.cpus}", "-c", f"limits.memory={spec.memory_gib}GiB"]
            if spec.disk_gib:
                arguments += ["-d", f"{ROOT_DEVICE},size={spec.disk_gib}GiB"]
            self.incus(*arguments)
        finally:
            self.incus("image", "delete", alias, check=False)
        self.configure(spec.name, spec)

    def address(self, name: str) -> str | None:
        instance = self.instance(name)
        found = addresses_of(instance) if instance else []
        retval = found[0] if found else None
        return retval

    def console_command(self, name: str) -> list[str]:
        retlist = [INCUS, "console", name, "--type=console"]
        return retlist

    def resources(self, name: str) -> tuple[int, int] | None:
        instance = self.instance(name)
        retval = None
        if instance is not None:
            settings = instance.get("expanded_config") or instance.get("config") or {}
            cpus = settings.get("limits.cpu")
            memory = memory_gib_of(settings.get("limits.memory"))
            if cpus and memory is not None:
                retval = (int(cpus), memory)
        return retval

    def set_resources(self, name: str, cpus: int | None, memory_gib: int | None) -> None:
        settings = []
        if cpus is not None:
            settings.append(f"limits.cpu={cpus}")
        if memory_gib is not None:
            settings.append(f"limits.memory={memory_gib}GiB")
        if settings:
            self.incus("config", "set", name, *settings)

    # --- images ------------------------------------------------------------------------------------

    def image_info(self, name: str) -> dict | None:
        retval = None
        listed = self.query("image", "list", alias_for(name)) or []
        for item in listed:
            for alias in item.get("aliases") or []:
                if alias.get("name") == alias_for(name):
                    retval = item
        return retval

    def image_exists(self, name: str) -> bool:
        retval = self.image_info(name) is not None
        return retval

    def image_location(self, name: str) -> str:
        retval = f"incus image {alias_for(name)}"
        return retval

    def image_size(self, name: str) -> tuple[int, int]:
        info = self.image_info(name)
        size = int(info.get("size") or 0) if info else 0
        retval = (size, size)
        return retval

    def keep_image(self, vm: str, name: str) -> str:
        """The stopped build VM published as the base image alias, replacing an older one."""
        self.incus("publish", vm, "--alias", alias_for(name), "--reuse")
        retval = self.image_location(name)
        return retval

    def remove_image(self, name: str) -> bool:
        retval = self.image_exists(name)
        if retval:
            self.incus("image", "delete", alias_for(name))
        return retval
