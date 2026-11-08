"""The Driver protocol: what a hypervisor must do for the control side (PLAN.md M2, M5).

A driver knows how to create a VM from a disk file or one of its own images plus a cloud-init
seed, start and stop it, keep a stopped VM's disk as a base image and make a VM from one, copy
a VM, open its serial console, change its resources, and say which addresses its network
offers. It does not know what a backend is (PLAN.md, "Principles": mechanism below, policy
above). Two drivers: UTM on the Mac (`control_driver_utm`, M2) and Incus on the Linux laptop
(`control_driver_incus`, M5).

The seed travels with ``create`` as a NoCloud ISO that cloud-init finds by its ``cidata``
label: UTM imports it into the bundle, Incus attaches it as a disk device. So there is no
separate deliver-seed call; a VM that needs a new seed is recreated, which is what disposable
backends are for.
"""

import dataclasses
import pathlib
import platform
import time
import typing

from sandboxed_devenv import common, config
from sandboxed_devenv.control_inventory import AddressPool

IMAGE_FILE_SUFFIX = ".img"
# VM states, UTM's words; the Incus driver maps its own onto them.
STOPPED = "stopped"
STARTING = "starting"
STARTED = "started"
PAUSED = "paused"
STOPPING = "stopping"
UNKNOWN = "unknown"


class DriverError(Exception):
    pass


@dataclasses.dataclass
class VmInfo:
    name: str
    state: str
    id: str | None = None


@dataclasses.dataclass
class VmSpec:
    """Everything `create` needs. The VM's disk comes from ``disk``, an image file the
    hypervisor takes a copy of or imports, or from ``image``, the name of a base image the
    driver keeps (`keep_image`); ``seed_dir`` holds NoCloud's ``user-data``, ``meta-data`` and,
    when the VM has a static address, ``network-config``. ``address`` is the static address the
    driver should give the VM where its network hands out addresses itself (Incus), ``disk_gib``
    the size of the root disk where the driver sets it rather than the file (Incus). A display
    only for a VM someone logs into, like desk."""

    name: str
    disk: pathlib.Path | None
    cpus: int
    memory_gib: int
    seed_dir: pathlib.Path
    display: bool = False
    image: str | None = None
    address: str | None = None
    disk_gib: int | None = None

    @property
    def memory_mib(self) -> int:
        retval = self.memory_gib * 1024
        return retval


def image_file(name: str) -> pathlib.Path:
    """Where a file-based driver keeps a base image: ``~/.sandboxed_devenv/images/<name>.img``."""
    retval = config.images_dir() / f"{name}{IMAGE_FILE_SUFFIX}"
    return retval


class Driver(typing.Protocol):
    name: str
    runner: common.Runner
    # The seed carries a static network-config (UTM's shared network has no static leases);
    # otherwise the driver hands the VM its address and the guest uses DHCP.
    static_network: bool
    # The build VM wants a raw disk file (UTM's Apple backend) rather than the QCOW2 cloud image.
    raw_disk: bool
    # CPUs and memory can change on a running VM.
    live_resources: bool

    def available(self) -> str | None:
        """None when this machine can drive VMs with it, else the reason it cannot."""
        ...

    def address_pool(self, user_config: config.UserConfig) -> AddressPool:
        """The addresses the driver's VM network offers to backends, and the build VM's."""
        ...

    def image_exists(self, name: str) -> bool:
        """Whether the driver holds the base image `keep_image` made under this name."""
        ...

    def image_location(self, name: str) -> str:
        """Where the base image is: a file path, an image alias."""
        ...

    def image_size(self, name: str) -> tuple[int, int]:
        """(allocated bytes, apparent bytes) of a base image, or (0, 0) when the driver cannot say."""
        ...

    def keep_image(self, vm: str, name: str) -> str:
        """A stopped VM's disk kept as the base image `name` (the VM itself is deleted by the
        caller); returns its location. `image build` ends with it."""
        ...

    def remove_image(self, name: str) -> bool:
        """Remove a base image; False when there was none."""
        ...

    def clone_from(self, source: str, spec: VmSpec) -> None:
        """A new VM from a stopped VM's disk with the spec's seed, resources and address; not
        started."""
        ...

    def exists(self, name: str) -> bool: ...

    def create(self, spec: VmSpec) -> None: ...

    def start(self, name: str) -> None: ...

    def stop(self, name: str, force: bool = False) -> None:
        """Ask the guest to shut down, or pull the plug with ``force``."""
        ...

    def delete(self, name: str) -> None: ...

    def clone(self, name: str, new_name: str) -> None: ...

    def list(self) -> list[VmInfo]: ...

    def state(self, name: str) -> str: ...

    def address(self, name: str) -> str | None:
        """The guest's address when the hypervisor can tell; None when only the seed knows it."""
        ...

    def console_command(self, name: str) -> list[str]:
        """The command that attaches a terminal to the VM's serial console."""
        ...

    def resources(self, name: str) -> tuple[int, int] | None:
        """(CPUs, memory in GiB) as the hypervisor has them, or None when it cannot say."""
        ...

    def set_resources(self, name: str, cpus: int | None, memory_gib: int | None) -> None:
        """Change a VM's CPUs and memory; on a stopped VM unless ``live_resources``."""
        ...


def wait_for_state(driver: Driver, name: str, wanted: str, timeout: float, interval: float = 2.0) -> float:
    """Seconds until the VM reports ``wanted``; DriverError after ``timeout``."""
    started = time.monotonic()
    while True:
        current = driver.state(name)
        if current == wanted or driver.runner.dry_run:
            break
        if time.monotonic() - started > timeout:
            raise DriverError(f"{name} is {current}, not {wanted}, after {common.format_duration(timeout)}")
        time.sleep(interval)
    retval = time.monotonic() - started
    return retval


def load_driver(runner: common.Runner | None = None) -> Driver:
    """The driver for this machine: UTM on macOS, Incus on Linux; DriverError when neither can
    run here."""
    runner = runner or common.Runner()
    system = platform.system()
    if system == "Darwin":
        from sandboxed_devenv import control_driver_utm

        retval = control_driver_utm.UtmDriver(runner=runner)
    elif system == "Linux":
        from sandboxed_devenv import control_driver_incus

        retval = control_driver_incus.IncusDriver(runner=runner)
    else:
        raise DriverError(f"no driver for {system}")
    reason = retval.available()
    if reason:
        raise DriverError(reason)
    return retval
