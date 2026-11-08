"""The UTM driver's command lines and parsing, without UTM; and the protocol module's helpers."""

import pathlib
import subprocess

import pytest

from sandboxed_devenv import control_driver, control_driver_utm
from sandboxed_devenv.control_driver import DriverError, VmSpec


class FakeRunner:
    dry_run = False
    verbose = False

    def __init__(self, outputs=None):
        self.commands = []
        self.inputs = []
        self.outputs = list(outputs or [])

    def run(self, command, **kwargs):
        self.commands.append(command)
        self.inputs.append(kwargs.get("input"))
        if self.outputs:
            status, stdout, stderr = self.outputs.pop(0)
        else:
            status, stdout, stderr = 0, "", ""
        retval = subprocess.CompletedProcess(command, status, stdout, stderr)
        return retval


LIST_OUTPUT = """UUID                                 Status   Name
C050B448-1496-4DB4-9F48-84A54028BCD7 started  backend
D6AAF7F5-E7B3-4394-A4B5-789D79E27042 stopped  idesbx-ide sandbox
"""


def test_parse_list_keeps_names_with_spaces():
    vms = control_driver_utm.parse_list(LIST_OUTPUT)
    assert [(vm.name, vm.state) for vm in vms] == [("backend", "started"), ("idesbx-ide sandbox", "stopped")]
    assert vms[0].id == "C050B448-1496-4DB4-9F48-84A54028BCD7"
    assert control_driver_utm.parse_list("UUID Status Name\n") == []


def test_bundle_paths_and_commands():
    assert control_driver_utm.bundle_path("x").name == "x.utm"
    assert control_driver_utm.config_plist("x").name == "config.plist"
    iso = control_driver_utm.seed_iso_command(pathlib.Path("/s"), pathlib.Path("/s.iso"))
    assert iso[:2] == ["hdiutil", "makehybrid"]
    assert iso[iso.index("-default-volume-name") + 1] == "cidata"
    assert iso[-1] == "/s"
    assert control_driver_utm.apfs_clone_command(pathlib.Path("/a"), pathlib.Path("/b")) == ["cp", "-c", "/a", "/b"]


def test_list_exists_and_state():
    runner = FakeRunner([(0, LIST_OUTPUT, ""), (0, LIST_OUTPUT, ""), (0, "started\n", ""), (1, "", "Error: Virtual machine not found.")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    assert [vm.name for vm in driver.list()] == ["backend", "idesbx-ide sandbox"]
    assert driver.exists("backend") is True
    assert driver.state("backend") == "started"
    with pytest.raises(DriverError):
        driver.state("nope")
    assert runner.commands[0] == ["utmctl", "list"]
    assert runner.commands[2] == ["utmctl", "status", "backend"]


def test_create_builds_the_iso_then_runs_the_applescript(tmp_path):
    disk = tmp_path / "backend-0.1.0.img"
    disk.write_bytes(b"")
    seed = tmp_path / "seed"
    seed.mkdir()
    runner = FakeRunner()
    driver = control_driver_utm.UtmDriver(runner=runner)
    driver.create(VmSpec(name="idesbx-image-build", disk=disk, cpus=2, memory_gib=4, seed_dir=seed))
    assert runner.commands[0][:2] == ["hdiutil", "makehybrid"]
    iso = runner.commands[0][runner.commands[0].index("-o") + 1]
    assert iso.endswith("idesbx-image-build-seed.iso")
    create = runner.commands[1]
    assert create[:2] == ["osascript", "-"]
    assert create[2:] == ["idesbx-image-build", "4096", "2", str(disk), iso, "no"]
    assert "make new virtual machine with properties {backend:apple" in runner.inputs[1]
    assert len(runner.commands) == 2


def test_create_with_a_display_enables_input_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(control_driver_utm, "UTM_DOCUMENTS", tmp_path)
    bundle = tmp_path / "desk.utm"
    bundle.mkdir()
    (bundle / "config.plist").write_text("<plist/>")
    disk = tmp_path / "desk.img"
    disk.write_bytes(b"")
    runner = FakeRunner()
    driver = control_driver_utm.UtmDriver(runner=runner)
    monkeypatch.setattr(driver, "reread_bundle", lambda name: None)
    driver.create(VmSpec(name="desk", disk=disk, cpus=4, memory_gib=8, seed_dir=tmp_path, display=True))
    assert runner.commands[1][-1] == "yes"
    replaced = [command[2] for command in runner.commands if command[:2] == ["plutil", "-replace"]]
    assert replaced == ["Virtualization.Keyboard", "Virtualization.Pointer", "Virtualization.ClipboardSharing"]


def test_create_refuses_a_missing_disk(tmp_path):
    driver = control_driver_utm.UtmDriver(runner=FakeRunner())
    with pytest.raises(DriverError):
        driver.create(VmSpec(name="x", disk=tmp_path / "missing.img", cpus=1, memory_gib=1, seed_dir=tmp_path))


def test_lifecycle_commands():
    runner = FakeRunner()
    driver = control_driver_utm.UtmDriver(runner=runner)
    driver.start("x")
    driver.stop("x")
    driver.stop("x", force=True)
    driver.clone("x", "y")
    driver.delete("x")
    assert runner.commands == [
        ["utmctl", "start", "x"],
        ["utmctl", "stop", "x", "--request"],
        ["utmctl", "stop", "x", "--force"],
        ["utmctl", "clone", "x", "--name", "y"],
        ["utmctl", "delete", "x"],
    ]
    assert driver.address("x") is None


def test_failed_utmctl_raises():
    driver = control_driver_utm.UtmDriver(runner=FakeRunner([(1, "", "boom")]))
    with pytest.raises(DriverError):
        driver.start("x")


def test_console_command_uses_the_serial_port_pty():
    runner = FakeRunner([(0, "/dev/ttys013\n", "")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    assert driver.console_command("backend") == ["screen", "/dev/ttys013"]
    assert runner.commands[0] == ["osascript", "-", "backend"]
    assert "first serial port" in runner.inputs[0]
    with pytest.raises(DriverError):
        control_driver_utm.UtmDriver(runner=FakeRunner([(0, "", "")])).serial_port("backend")


def test_export_disk_clones_the_first_drive_and_falls_back_to_a_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(control_driver_utm, "UTM_DOCUMENTS", tmp_path)
    data = tmp_path / "idesbx-image-build.utm" / "Data"
    data.mkdir(parents=True)
    (data.parent / "config.plist").write_text("<plist/>")
    (data / "backend-0.1.0.img").write_bytes(b"disk")
    runner = FakeRunner([(0, '{"Drive": [{"ImageName": "backend-0.1.0.img"}, {"ImageName": "seed.iso"}]}', ""),
                         (1, "", "cp: clonefile failed")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    dest = tmp_path / "out.img"
    driver.export_disk("idesbx-image-build", dest)
    assert runner.commands[0][:3] == ["plutil", "-convert", "json"]
    assert runner.commands[1] == ["cp", "-c", str(data / "backend-0.1.0.img"), str(dest)]
    assert dest.read_bytes() == b"disk"


def test_export_disk_without_drives():
    runner = FakeRunner([(0, '{"Drive": []}', "")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    with pytest.raises(DriverError):
        driver.disk_file("x")


def test_read_config_needs_the_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(control_driver_utm, "UTM_DOCUMENTS", tmp_path)
    with pytest.raises(DriverError):
        control_driver_utm.UtmDriver(runner=FakeRunner()).read_config("none")


def test_set_resources_only_on_a_stopped_vm():
    runner = FakeRunner([(0, "started\n", "")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    with pytest.raises(DriverError):
        driver.set_resources("x", cpus=2, memory_gib=None)
    runner = FakeRunner([(0, "stopped\n", ""), (0, "", "")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    driver.set_resources("x", cpus=2, memory_gib=4)
    assert runner.commands[1] == ["osascript", "-", "x", "4096", "2"]
    assert "update configuration" in runner.inputs[1]
    driver.set_resources("x", cpus=None, memory_gib=None)
    assert len(runner.commands) == 2


def test_available_off_macos(monkeypatch):
    monkeypatch.setattr(control_driver_utm.platform, "system", lambda: "Linux")
    assert "macOS" in control_driver_utm.UtmDriver(runner=FakeRunner()).available()


def test_load_driver_on_linux_points_at_m5(monkeypatch):
    monkeypatch.setattr(control_driver.platform, "system", lambda: "Linux")
    with pytest.raises(DriverError) as raised:
        control_driver.load_driver()
    assert "M5" in str(raised.value)


def test_wait_for_state(monkeypatch):
    monkeypatch.setattr(control_driver.time, "sleep", lambda seconds: None)
    runner = FakeRunner([(0, "stopping\n", ""), (0, "stopped\n", "")])
    driver = control_driver_utm.UtmDriver(runner=runner)
    assert control_driver.wait_for_state(driver, "x", control_driver.STOPPED, timeout=10, interval=0) >= 0
    clock = iter([0, 0, 100, 100, 100])
    monkeypatch.setattr(control_driver.time, "monotonic", lambda: next(clock))
    driver = control_driver_utm.UtmDriver(runner=FakeRunner([(0, "started\n", ""), (0, "started\n", "")]))
    with pytest.raises(DriverError):
        control_driver.wait_for_state(driver, "x", control_driver.STOPPED, timeout=10, interval=0)


def test_vm_spec_memory():
    assert VmSpec(name="x", disk=pathlib.Path("/d"), cpus=1, memory_gib=8, seed_dir=pathlib.Path("/s")).memory_mib == 8192
