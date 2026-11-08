"""The UTM driver for the Mac rig: utmctl, osascript, plutil, hdiutil (control role, M2).

Ported from the rig's create-vms.sh and make-seed.sh, deleted at M2 (summary 4.9). What the rig learned
about UTM 4.7.5 with the Apple Virtualization backend, kept here with dates:

- 8 October 2026: a removable drive is referenced by path, and UTM, a sandboxed app, cannot
  read a path handed to it by script ("Cannot access resource" at the first start). A drive
  with a source and no `removable:true` is imported, that is copied, into the VM bundle. Both
  the disk and the seed ISO go in that way; the disk first, so EFI boots from it, and the seed
  found by cloud-init through its `cidata` label on whatever block device it lands on. The
  copy means the caller's files stay pristine and can be removed once `create` returns.
- 8 October 2026: the Apple backend takes raw disk images only.
- 9 October 2026: a scripted VM gets Keyboard and Pointer "Disabled", unreachable through the
  scripting interface; a VM with a display gets them set in the bundle's config.plist with
  plutil, then UTM must re-read the bundle. Newer UTM has a `reload configuration` command;
  4.7.5 does not, so the fallback quits and relaunches UTM, which re-reads every bundle. Only
  stopped VMs are affected; the fallback expects no VM to be running.
- 9 October 2026: `utmctl start` is headless, nothing opens in UTM's window; `utmctl attach`
  is not implemented. The console is `screen` on the pseudo-terminal that the AppleScript
  `address of first serial port` reports; the port exists only while the VM runs.
- 9 October 2026: `utmctl stop --request` once hung for more than two minutes on a systemd
  stop job; callers wait, then force.
- The Apple backend has no guest agent for Linux, so UTM cannot report a guest's address:
  `address()` is None, and the seed's static address is what the inventory records.
- 8 November 2026: a VM made by script with no `serial ports` or `network interfaces` given
  gets one shared network interface and one PTTY serial port by default, as the bundle's
  config.plist shows.
- `update configuration` exists in 4.7.5 for a stopped VM; whether a partial record keeps the
  rest of the configuration is verified with `resources` (PLAN.md M2).
"""

import dataclasses
import json
import os
import pathlib
import platform
import shutil
import subprocess
import tempfile
import time

from sandboxed_devenv import common, config, control_driver, control_inventory
from sandboxed_devenv.control_driver import DriverError, VmInfo, VmSpec

DRIVER_NAME = "utm"
UTMCTL = "utmctl"
UTM_APP = pathlib.Path("/Applications/UTM.app")
UTM_PROCESS = "UTM"
# Where UTM keeps each VM's bundle: config.plist and Data/ with the imported drives.
UTM_DOCUMENTS = pathlib.Path.home() / "Library" / "Containers" / "com.utmapp.UTM" / "Data" / "Documents"
BUNDLE_SUFFIX = ".utm"
SEED_VOLUME_NAME = "cidata"
UTM_QUIT_TIMEOUT = 20
UTM_RELAUNCH_WAIT = 4

CREATE_SCRIPT = '''
on run argv
  set vmName to item 1 of argv
  set memMiB to (item 2 of argv) as integer
  set cores to (item 3 of argv) as integer
  set diskFile to POSIX file (item 4 of argv)
  set seedFile to POSIX file (item 5 of argv)
  set wantDisplay to (item 6 of argv) is "yes"
  tell application "UTM"
    if exists virtual machine named vmName then
      error "a VM named " & vmName & " already exists"
    end if
    -- Drives with a source and no removable:true are imported (copied) into the bundle.
    -- The disk comes first so EFI boots from it; the seed has no boot loader and is found by
    -- cloud-init through its cidata label.
    set config to {name:vmName, memory:memMiB, cpu cores:cores, drives:{{source:diskFile}, {source:seedFile}}}
    if wantDisplay then
      set config to config & {displays:{{dynamic resolution:true}}}
    end if
    make new virtual machine with properties {backend:apple, configuration:config}
  end tell
end run
'''

SERIAL_PORT_SCRIPT = '''
on run argv
  tell application "UTM"
    get address of first serial port of virtual machine named (item 1 of argv)
  end tell
end run
'''

RESOURCES_SCRIPT = '''
on run argv
  set vmName to item 1 of argv
  set memMiB to (item 2 of argv) as integer
  set cores to (item 3 of argv) as integer
  tell application "UTM"
    set vm to virtual machine named vmName
    if memMiB > 0 and cores > 0 then
      update configuration vm with {memory:memMiB, cpu cores:cores}
    else if memMiB > 0 then
      update configuration vm with {memory:memMiB}
    else
      update configuration vm with {cpu cores:cores}
    end if
  end tell
end run
'''

RELOAD_SCRIPT = '''
on run argv
  tell application "UTM"
    set vm to virtual machine named (item 1 of argv)
    reload configuration vm
  end tell
end run
'''


def parse_list(text: str) -> list[VmInfo]:
    """`utmctl list` prints a header, then one line per VM: UUID, status, name (which may hold spaces)."""
    retlist = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            retlist.append(VmInfo(name=parts[2].strip(), state=parts[1], id=parts[0]))
    return retlist


def bundle_path(name: str) -> pathlib.Path:
    retval = UTM_DOCUMENTS / f"{name}{BUNDLE_SUFFIX}"
    return retval


def config_plist(name: str) -> pathlib.Path:
    retval = bundle_path(name) / "config.plist"
    return retval


def seed_iso_command(seed_dir: pathlib.Path, iso: pathlib.Path) -> list[str]:
    """An ISO 9660 image with Joliet names and the volume label cloud-init's NoCloud source looks for."""
    retlist = ["hdiutil", "makehybrid", "-quiet", "-iso", "-joliet", "-default-volume-name", SEED_VOLUME_NAME,
               "-o", str(iso), str(seed_dir)]
    return retlist


def apfs_clone_command(source: pathlib.Path, dest: pathlib.Path) -> list[str]:
    """`cp -c` asks for a clone: instant and block-sharing on APFS, an error elsewhere."""
    retlist = ["cp", "-c", str(source), str(dest)]
    return retlist


@dataclasses.dataclass
class UtmDriver:
    name: str = DRIVER_NAME
    runner: common.Runner = dataclasses.field(default_factory=common.Runner)

    # --- plumbing ---------------------------------------------------------------------------------

    def utmctl(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        kwargs = {}
        if capture:
            kwargs["capture_output"] = True
        retval = self.runner.run([UTMCTL, *args], **kwargs)
        return retval

    def osascript(self, script: str, *args: str) -> str:
        """Run an AppleScript from stdin with arguments, returning its output; DriverError on failure."""
        completed = self.runner.run(["osascript", "-", *args], input=script, capture_output=True)
        if completed.returncode != 0:
            raise DriverError(f"osascript failed with status {completed.returncode}: {(completed.stderr or '').strip()}")
        retval = (completed.stdout or "").strip()
        return retval

    def read_config(self, name: str) -> dict:
        """The bundle's config.plist as a dictionary (plutil converts it to JSON)."""
        plist = config_plist(name)
        if not plist.is_file():
            raise DriverError(f"no bundle for {name} at {plist}")
        completed = self.runner.run(["plutil", "-convert", "json", "-o", "-", str(plist)], capture_output=True)
        if completed.returncode != 0:
            raise DriverError(f"plutil could not read {plist}: {(completed.stderr or '').strip()}")
        retval = json.loads(completed.stdout) if completed.stdout else {}
        return retval

    # --- the protocol -----------------------------------------------------------------------------

    static_network = True
    raw_disk = True
    live_resources = False

    def address_pool(self, user_config: config.UserConfig) -> control_inventory.AddressPool:
        retval = control_inventory.utm_pool(user_config)
        return retval

    def image_exists(self, name: str) -> bool:
        retval = control_driver.image_file(name).is_file()
        return retval

    def image_location(self, name: str) -> str:
        retval = str(control_driver.image_file(name))
        return retval

    def image_size(self, name: str) -> tuple[int, int]:
        path = control_driver.image_file(name)
        retval = (0, 0)
        if path.is_file():
            status = path.stat()
            retval = (status.st_blocks * 512, status.st_size)
        return retval

    def keep_image(self, vm: str, name: str) -> str:
        """The stopped build VM's disk cloned out of its bundle into the images directory."""
        final = control_driver.image_file(name)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(f"{final.name}.tmp")
        self.export_disk(vm, temporary)
        if not self.runner.dry_run:
            os.replace(temporary, final)
        retval = str(final)
        return retval

    def remove_image(self, name: str) -> bool:
        path = control_driver.image_file(name)
        retval = path.exists()
        if retval and not self.runner.dry_run:
            path.unlink()
        return retval

    def clone_from(self, source: str, spec: VmSpec) -> None:
        """The stopped source VM's disk cloned out of its bundle (APFS shares the blocks), then a
        VM made from the copy with the spec's seed; the copy is removed once UTM has imported it."""
        with tempfile.TemporaryDirectory(prefix="sandboxed-devenv-clone-", dir=config.images_dir()) as temporary:
            disk = pathlib.Path(temporary) / f"{spec.name}{control_driver.IMAGE_FILE_SUFFIX}"
            self.export_disk(source, disk)
            self.create(dataclasses.replace(spec, disk=disk, image=None))

    def available(self) -> str | None:
        retval = None
        if platform.system() != "Darwin":
            retval = f"the UTM driver runs on macOS; this is {platform.system()}"
        elif not shutil.which(UTMCTL):
            retval = f"{UTMCTL} is not on the PATH (brew install --cask utm links it into /opt/homebrew/bin)"
        elif not shutil.which("hdiutil"):
            retval = "hdiutil is missing, which every macOS has; something is wrong with the PATH"
        return retval

    def list(self) -> list[VmInfo]:
        completed = self.utmctl("list", capture=True)
        if completed.returncode != 0:
            raise DriverError(f"utmctl list failed with status {completed.returncode}: {(completed.stderr or '').strip()}")
        retlist = parse_list(completed.stdout or "")
        return retlist

    def exists(self, name: str) -> bool:
        retval = False
        for info in self.list():
            if info.name == name:
                retval = True
                break
        return retval

    def state(self, name: str) -> str:
        completed = self.utmctl("status", name, capture=True)
        if completed.returncode != 0:
            raise DriverError(f"no VM named {name}: {(completed.stderr or '').strip() or 'utmctl status failed'}")
        retval = (completed.stdout or "").strip() or "unknown"
        return retval

    def create(self, spec: VmSpec) -> None:
        """Build the seed ISO, then make the VM through AppleScript; UTM copies the disk (the file
        given, or a base image kept under the images directory) and the ISO into the bundle. A VM
        with a display also gets its input devices enabled."""
        if spec.disk is None and spec.image is not None:
            spec = dataclasses.replace(spec, disk=control_driver.image_file(spec.image))
        if spec.disk is None:
            raise DriverError(f"{spec.name}: neither a disk file nor a base image to make the VM from")
        if not spec.disk.is_file() and not self.runner.dry_run:
            raise DriverError(f"no disk at {spec.disk}")
        with tempfile.TemporaryDirectory(prefix="sandboxed-devenv-seed-") as temporary:
            iso = pathlib.Path(temporary) / f"{spec.name}-seed.iso"
            completed = self.runner.run(seed_iso_command(spec.seed_dir, iso), capture_output=True)
            if completed.returncode != 0:
                raise DriverError(f"hdiutil failed with status {completed.returncode}: {(completed.stderr or '').strip()}")
            self.osascript(CREATE_SCRIPT, spec.name, str(spec.memory_mib), str(spec.cpus), str(spec.disk), str(iso),
                           "yes" if spec.display else "no")
        if spec.display:
            self.enable_input_devices(spec.name)
            self.reread_bundle(spec.name)

    def start(self, name: str) -> None:
        completed = self.utmctl("start", name)
        if completed.returncode != 0:
            raise DriverError(f"utmctl start {name} failed with status {completed.returncode}")

    def stop(self, name: str, force: bool = False) -> None:
        completed = self.utmctl("stop", name, "--force" if force else "--request")
        if completed.returncode != 0:
            raise DriverError(f"utmctl stop {name} failed with status {completed.returncode}")

    def delete(self, name: str) -> None:
        completed = self.utmctl("delete", name)
        if completed.returncode != 0:
            raise DriverError(f"utmctl delete {name} failed with status {completed.returncode}")

    def clone(self, name: str, new_name: str) -> None:
        completed = self.utmctl("clone", name, "--name", new_name)
        if completed.returncode != 0:
            raise DriverError(f"utmctl clone {name} failed with status {completed.returncode}")

    def address(self, name: str) -> str | None:
        retval = None
        return retval

    def serial_port(self, name: str) -> str:
        """The pseudo-terminal of the running VM's first serial port, such as /dev/ttys013."""
        retval = self.osascript(SERIAL_PORT_SCRIPT, name)
        if not retval.startswith("/dev/") and not self.runner.dry_run:
            raise DriverError(f"UTM reported no serial port for {name}: {retval!r} (is it running?)")
        return retval

    def console_command(self, name: str) -> list[str]:
        retlist = ["screen", self.serial_port(name)]
        return retlist

    def disk_file(self, name: str) -> pathlib.Path:
        """The first drive's file inside the bundle; UTM keeps the imported file's name."""
        drives = self.read_config(name).get("Drive") or []
        if not drives:
            raise DriverError(f"{name} has no drives")
        image_name = drives[0].get("ImageName")
        if not image_name:
            raise DriverError(f"{name}'s first drive has no image file")
        retval = bundle_path(name) / "Data" / image_name
        return retval

    def export_disk(self, name: str, dest: pathlib.Path) -> None:
        """An APFS clone of the stopped VM's disk, or a plain copy where cloning is refused."""
        if self.runner.dry_run:
            print(f"would clone the disk of {name} to {dest}")
            return
        source = self.disk_file(name)
        if not source.is_file():
            raise DriverError(f"no disk file at {source}")
        if dest.exists():
            dest.unlink()
        completed = self.runner.run(apfs_clone_command(source, dest), capture_output=True)
        if completed.returncode != 0:
            shutil.copyfile(source, dest)

    def resources(self, name: str) -> tuple[int, int] | None:
        """From the bundle's config.plist: System.CPUCount and System.MemorySize (MiB)."""
        system = self.read_config(name).get("System") or {}
        retval = None
        if "CPUCount" in system and "MemorySize" in system:
            retval = (int(system["CPUCount"]), int(system["MemorySize"]) // 1024)
        return retval

    def set_resources(self, name: str, cpus: int | None, memory_gib: int | None) -> None:
        if cpus is None and memory_gib is None:
            return
        state = self.state(name)
        if state != "stopped" and not self.runner.dry_run:
            raise DriverError(f"{name} is {state}; UTM changes CPUs and memory only on a stopped VM")
        self.osascript(RESOURCES_SCRIPT, name, str((memory_gib or 0) * 1024), str(cpus or 0))

    # --- the display VM's input devices -----------------------------------------------------------

    def enable_input_devices(self, name: str) -> None:
        """Keyboard, pointer and clipboard sharing in config.plist: a scripted VM has them off, and
        the scripting interface cannot set them (9 October 2026)."""
        plist = config_plist(name)
        if not plist.is_file() and not self.runner.dry_run:
            raise DriverError(f"no bundle for {name} at {plist}")
        for key, kind, value in (("Virtualization.Keyboard", "-string", "Generic"),
                                 ("Virtualization.Pointer", "-string", "Mouse"),
                                 ("Virtualization.ClipboardSharing", "-bool", "true")):
            completed = self.runner.run(["plutil", "-replace", key, kind, value, str(plist)], capture_output=True)
            if completed.returncode != 0:
                raise DriverError(f"plutil -replace {key} failed: {(completed.stderr or '').strip()}")

    def reread_bundle(self, name: str) -> None:
        """Make UTM re-read a bundle edited on disk: the reload command where it exists, else quit
        and relaunch UTM (4.7.5), which expects no VM to be running."""
        try:
            self.osascript(RELOAD_SCRIPT, name)
            return
        except DriverError:
            pass
        print("this UTM cannot reload one configuration; restarting UTM so it re-reads the bundles", flush=True)
        self.runner.run(["osascript", "-e", 'tell application "UTM" to quit'], capture_output=True)
        if not self.runner.dry_run:
            waited = 0
            while waited < UTM_QUIT_TIMEOUT:
                if self.runner.run(["pgrep", "-xq", UTM_PROCESS], capture_output=True).returncode != 0:
                    break
                time.sleep(1)
                waited += 1
        self.runner.run(["open", "-a", UTM_PROCESS], capture_output=True)
        if not self.runner.dry_run:
            time.sleep(UTM_RELAUNCH_WAIT)
