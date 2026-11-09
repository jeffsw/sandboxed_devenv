"""The Incus driver's command lines and parsing against recorded `incus` output, without Incus."""

import json
import pathlib
import subprocess

import pytest

from sandboxed_devenv import common, config, control_driver, control_driver_incus
from sandboxed_devenv.control_driver import DriverError, VmSpec


class FakeRunner:
    dry_run = False
    verbose = False

    def __init__(self, outputs=None):
        self.commands = []
        self.outputs = list(outputs or [])

    def run(self, command, **kwargs):
        self.commands.append(command)
        if self.outputs:
            status, stdout, stderr = self.outputs.pop(0)
        else:
            status, stdout, stderr = 0, "", ""
        retval = subprocess.CompletedProcess(command, status, stdout, stderr)
        return retval


INSTANCES = json.dumps([
    {"name": "idesbx-p", "status": "Running", "type": "virtual-machine",
     "config": {"limits.cpu": "4", "limits.memory": "8GiB"}, "expanded_config": {"limits.cpu": "4", "limits.memory": "8GiB"},
     "devices": {"cidata": {"type": "disk", "source": "/home/jsw/.sandboxed_devenv/seeds/idesbx-p.iso"},
                 "eth0": {"type": "nic", "network": "incusbr0", "ipv4.address": "10.42.0.20"}},
     "state": {"network": {"lo": {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "local"}]},
                           "enp5s0": {"addresses": [{"family": "inet", "address": "10.42.0.20", "scope": "global"},
                                                    {"family": "inet6", "address": "fe80::1", "scope": "link"}]}}}},
    {"name": "idesbx-q", "status": "Stopped", "type": "virtual-machine", "config": {}, "expanded_config": {}, "devices": {},
     "state": None},
])
IMAGES = json.dumps([{"fingerprint": "abc", "size": 3221225472, "aliases": [{"name": "sandboxed-devenv/backend-0.1.0"}]}])


def driver_with(outputs, user_config=None) -> tuple[control_driver_incus.IncusDriver, FakeRunner]:
    runner = FakeRunner(outputs)
    retval = (control_driver_incus.IncusDriver(runner=runner, user_config=user_config or config.UserConfig()), runner)
    return retval


def test_parsing_states_addresses_and_memory():
    instances = control_driver_incus.parse_list(INSTANCES)
    assert control_driver_incus.state_of(instances[0]) == control_driver.STARTED
    assert control_driver_incus.state_of(instances[1]) == control_driver.STOPPED
    assert control_driver_incus.state_of({"status": "Frozen"}) == control_driver.PAUSED
    assert control_driver_incus.addresses_of(instances[0]) == ["10.42.0.20"]
    assert control_driver_incus.addresses_of(instances[1]) == []
    assert control_driver_incus.parse_list("") == []
    assert control_driver_incus.memory_gib_of("8GiB") == 8
    assert control_driver_incus.memory_gib_of("4096MiB") == 4
    assert control_driver_incus.memory_gib_of(str(2 << 30)) == 2
    assert control_driver_incus.memory_gib_of(None) is None
    assert control_driver_incus.alias_for("backend-0.1.0") == "sandboxed-devenv/backend-0.1.0"


def test_list_exists_state_address_and_resources():
    driver, runner = driver_with([(0, INSTANCES, ""), (0, INSTANCES, ""), (0, INSTANCES, ""), (0, INSTANCES, ""), (0, "[]", "")])
    assert [(vm.name, vm.state) for vm in driver.list()] == [("idesbx-p", "started"), ("idesbx-q", "stopped")]
    assert runner.commands[0] == ["incus", "list", "--format", "json"]
    assert driver.state("idesbx-p") == "started"
    assert driver.address("idesbx-p") == "10.42.0.20"
    assert driver.resources("idesbx-p") == (4, 8)
    with pytest.raises(DriverError):
        driver.state("idesbx-x")
    assert not driver.static_network and not driver.raw_disk and driver.live_resources


def test_address_pool_from_the_bridge_or_the_config():
    driver, runner = driver_with([(0, "10.42.0.1/24\n", "")])
    pool = driver.address_pool(config.UserConfig())
    assert (pool.subnet, pool.start, pool.build_address) == ("10.42.0.0/24", "10.42.0.20", "10.42.0.19")
    assert pool.reserved == {"10.42.0.1", "10.42.0.19"}
    assert runner.commands[0] == ["incus", "network", "get", "incusbr0", "ipv4.address"]
    configured = config.UserConfig(incus=config.IncusConfig(subnet="10.9.0.0/24", address_pool_start="10.9.0.100", network="br1"))
    driver, runner = driver_with([])
    pool = driver.address_pool(configured)
    assert (pool.start, pool.build_address) == ("10.9.0.100", "10.9.0.99")
    assert runner.commands == []
    driver, runner = driver_with([(0, "none\n", "")])
    with pytest.raises(DriverError):
        driver.address_pool(config.UserConfig())


def test_create_from_an_image_alias_adds_the_seed_the_lease_and_the_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: "/usr/bin/genisoimage" if name == "genisoimage" else None)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "user-data").write_text("#cloud-config\n")
    driver, runner = driver_with([(0, "", ""), (0, "", ""), (0, "[]", ""), (0, "", ""), (0, "", ""), (0, "", "")])
    driver.create(VmSpec(name="idesbx-p", disk=None, cpus=2, memory_gib=4, seed_dir=seed, image="backend-0.1.0",
                         address="10.42.0.20", disk_gib=32))
    assert runner.commands[0] == ["incus", "init", "sandboxed-devenv/backend-0.1.0", "idesbx-p", "--vm", "--storage", "sandboxed-devenv",
                                  "-c", "limits.cpu=2", "-c", "limits.memory=4GiB", "-d", "root,size=32GiB"]
    assert runner.commands[1][:2] == ["genisoimage", "-quiet"] and runner.commands[1][-1] == str(seed)
    iso = str(tmp_path / "seeds" / "idesbx-p.iso")
    assert runner.commands[3] == ["incus", "config", "device", "add", "idesbx-p", "cidata", "disk", f"source={iso}"]
    assert runner.commands[4] == ["incus", "config", "set", "idesbx-p", "limits.cpu=2", "limits.memory=4GiB"]
    assert runner.commands[5] == ["incus", "config", "device", "override", "idesbx-p", "eth0", "ipv4.address=10.42.0.20"]


def test_create_from_a_disk_imports_a_temporary_image(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: "/usr/bin/xorrisofs" if name == "xorrisofs" else None)
    disk = tmp_path / "ubuntu.img"
    disk.write_bytes(b"qcow2")
    seed = tmp_path / "seed"
    seed.mkdir()
    driver, runner = driver_with([])
    driver.create(VmSpec(name="idesbx-image-build", disk=disk, cpus=2, memory_gib=4, seed_dir=seed, address="10.42.0.19"))
    assert runner.commands[0][:3] == ["incus", "image", "import"] and runner.commands[0][-2:] == ["--alias", "sandboxed-devenv-import-idesbx-image-build"]
    assert runner.commands[1][:3] == ["incus", "init", "sandboxed-devenv-import-idesbx-image-build"]
    assert runner.commands[2] == ["incus", "image", "delete", "sandboxed-devenv-import-idesbx-image-build"]
    assert runner.commands[3][0] == "xorrisofs"
    with pytest.raises(DriverError):
        driver.create(VmSpec(name="x", disk=None, cpus=1, memory_gib=1, seed_dir=seed))


def test_lifecycle_clone_console_and_images(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: "/usr/bin/genisoimage")
    seed = tmp_path / "seed"
    seed.mkdir()
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "idesbx-p.iso").write_bytes(b"iso")
    driver, runner = driver_with([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")])
    driver.start("idesbx-p")
    driver.stop("idesbx-p")
    driver.stop("idesbx-p", force=True)
    driver.delete("idesbx-p")
    assert runner.commands == [["incus", "start", "idesbx-p"], ["incus", "stop", "idesbx-p"], ["incus", "stop", "idesbx-p", "--force"],
                               ["incus", "delete", "idesbx-p", "--force"]]
    assert not (tmp_path / "seeds" / "idesbx-p.iso").exists()
    assert driver.console_command("idesbx-p") == ["incus", "console", "idesbx-p", "--type=console"]
    driver, runner = driver_with([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", ""), (0, INSTANCES, ""), (0, "", ""), (0, "", ""), (0, "", "")])
    driver.clone_from("idesbx-p", VmSpec(name="idesbx-q", disk=None, cpus=1, memory_gib=2, seed_dir=seed, address="10.42.0.21",
                                        disk_gib=32))
    assert runner.commands[0] == ["incus", "publish", "idesbx-p", "--alias", "sandboxed-devenv-import-clone-idesbx-q", "--reuse"]
    assert runner.commands[1] == ["incus", "init", "sandboxed-devenv-import-clone-idesbx-q", "idesbx-q", "--vm", "--storage", "sandboxed-devenv",
                                  "-c", "limits.cpu=1", "-c", "limits.memory=2GiB", "-d", "root,size=32GiB"]
    assert runner.commands[2] == ["incus", "image", "delete", "sandboxed-devenv-import-clone-idesbx-q"]
    # idesbx-q as recorded has no devices of its own yet, so the nic is overridden from the profile.
    assert ["incus", "config", "device", "override", "idesbx-q", "eth0", "ipv4.address=10.42.0.21"] in runner.commands
    driver, runner = driver_with([(0, "", ""), (0, "", ""), (0, "", ""), (0, "", ""), (0, INSTANCES, ""), (0, "", ""), (0, "", ""), (0, "", "")])
    driver.clone_from("idesbx-x", VmSpec(name="idesbx-p", disk=None, cpus=1, memory_gib=2, seed_dir=seed, address="10.42.0.22"))
    assert ["incus", "config", "device", "remove", "idesbx-p", "cidata"] in runner.commands
    assert ["incus", "config", "device", "set", "idesbx-p", "eth0", "ipv4.address=10.42.0.22"] in runner.commands
    driver, runner = driver_with([(0, IMAGES, ""), (0, IMAGES, ""), (0, "", ""), (0, IMAGES, ""), (0, "", "")])
    assert driver.image_exists("backend-0.1.0") is True
    assert driver.image_size("backend-0.1.0") == (3221225472, 3221225472)
    assert driver.keep_image("idesbx-image-build", "backend-0.1.0") == "incus image sandboxed-devenv/backend-0.1.0"
    assert runner.commands[2] == ["incus", "publish", "idesbx-image-build", "--alias", "sandboxed-devenv/backend-0.1.0", "--reuse"]
    assert driver.remove_image("backend-0.1.0") is True
    assert runner.commands[4] == ["incus", "image", "delete", "sandboxed-devenv/backend-0.1.0"]
    driver, runner = driver_with([(0, "[]", "")])
    assert driver.image_exists("backend-0.1.0") is False
    driver, runner = driver_with([(0, "", "")])
    driver.set_resources("idesbx-p", cpus=6, memory_gib=None)
    assert runner.commands[0] == ["incus", "config", "set", "idesbx-p", "limits.cpu=6"]


def test_available_needs_linux_incus_and_a_daemon(monkeypatch):
    monkeypatch.setattr(control_driver_incus.platform, "system", lambda: "Darwin")
    assert "Linux" in driver_with([])[0].available()
    monkeypatch.setattr(control_driver_incus.platform, "system", lambda: "Linux")
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: None)
    assert "not on the PATH" in driver_with([])[0].available()
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: "/usr/bin/incus")
    driver, runner = driver_with([(1, "", "Error: Get http://unix.socket: permission denied")])
    assert "incus-admin" in driver.available()
    driver, runner = driver_with([(0, "config: {}", "")])
    assert driver.available() is None
    monkeypatch.setattr(control_driver.platform, "system", lambda: "Linux")
    monkeypatch.setattr(control_driver_incus.IncusDriver, "available", lambda self: "no daemon here")
    with pytest.raises(DriverError, match="no daemon here"):
        control_driver.load_driver()
    monkeypatch.setattr(control_driver_incus.IncusDriver, "available", lambda self: None)
    assert isinstance(control_driver.load_driver(), control_driver_incus.IncusDriver)


def test_seed_iso_command_needs_a_tool(monkeypatch):
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: None)
    with pytest.raises(DriverError):
        control_driver_incus.seed_iso_command(pathlib.Path("/s"), pathlib.Path("/s.iso"))
    monkeypatch.setattr(control_driver_incus.shutil, "which", lambda name: "/usr/bin/mkisofs" if name == "mkisofs" else None)
    assert control_driver_incus.seed_iso_command(pathlib.Path("/s"), pathlib.Path("/s.iso"))[0] == "mkisofs"


def test_queries_run_under_a_dry_run_and_changes_do_not(capsys):
    """`new --dry-run` on the laptop failed on the bridge address (8 November): a dry run must
    still read Incus, or the plan it prints is wrong. Only commands that change something are
    held back."""

    class DryRunner(FakeRunner):
        dry_run = True

        def run(self, command, **kwargs):
            self.queries.append(kwargs.get("query", False))
            retval = super().run(command, **kwargs)
            return retval

    runner = DryRunner([(0, INSTANCES, ""), (0, "10.42.0.1/24\n", ""), (0, "", "")])
    runner.queries = []
    driver = control_driver_incus.IncusDriver(runner=runner, user_config=config.UserConfig())
    assert driver.exists("idesbx-p") is True
    assert driver.address_pool(config.UserConfig()).subnet == "10.42.0.0/24"
    driver.start("idesbx-p")
    assert runner.queries == [True, True, False]
    real = common.Runner(dry_run=True)
    assert real.run(["false"]).returncode == 0
    assert "would run: false" in capsys.readouterr().out
    assert real.run(["false"], query=True, capture_output=True).returncode == 1
    assert capsys.readouterr().out == ""
