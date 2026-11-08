"""The Driver protocol: what a hypervisor must do for the control side (PLAN.md M2).

A driver knows how to create a VM from a raw disk and a cloud-init seed, start and stop it,
find its address where the hypervisor can tell, open its serial console and take its disk
out again. It does not know what a backend is (PLAN.md, "Principles": mechanism below,
policy above). Two drivers are planned: UTM on the Mac (`control_driver_utm`, M2) and Incus on
the Linux laptop (`control_driver_incus`, M5).

The seed travels with ``create``: UTM imports drives only when the VM is made, as an ISO that
cloud-init finds by its ``cidata`` label, and Incus takes the documents as instance config
keys at ``init``. So there is no separate deliver-seed call; a VM that needs a new seed is
recreated, which is what disposable backends are for.
"""

import dataclasses
import pathlib
import platform
import time
import typing

from sandboxed_devenv import common

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
    """Everything `create` needs. ``disk`` is a raw image the hypervisor takes a copy of or
    attaches; ``seed_dir`` holds NoCloud's ``user-data``, ``meta-data`` and, when the VM has a
    static address, ``network-config``. A display only for a VM someone logs into, like desk."""

    name: str
    disk: pathlib.Path
    cpus: int
    memory_gib: int
    seed_dir: pathlib.Path
    display: bool = False

    @property
    def memory_mib(self) -> int:
        retval = self.memory_gib * 1024
        return retval


class Driver(typing.Protocol):
    name: str
    runner: common.Runner

    def available(self) -> str | None:
        """None when this machine can drive VMs with it, else the reason it cannot."""
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

    def export_disk(self, name: str, dest: pathlib.Path) -> None:
        """A copy of the stopped VM's first disk at ``dest``; `image build` ends with it."""
        ...

    def resources(self, name: str) -> tuple[int, int] | None:
        """(CPUs, memory in GiB) as the hypervisor has them, or None when it cannot say."""
        ...

    def set_resources(self, name: str, cpus: int | None, memory_gib: int | None) -> None:
        """Change a stopped VM's CPUs and memory."""
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
    """The driver for this machine: UTM on macOS, Incus on Linux (M5); DriverError when neither
    can run here."""
    runner = runner or common.Runner()
    system = platform.system()
    if system == "Darwin":
        from sandboxed_devenv import control_driver_utm

        retval = control_driver_utm.UtmDriver(runner=runner)
    elif system == "Linux":
        raise DriverError("the Incus driver arrives at M5 (PLAN.md); no driver runs on Linux yet")
    else:
        raise DriverError(f"no driver for {system}")
    reason = retval.available()
    if reason:
        raise DriverError(reason)
    return retval
