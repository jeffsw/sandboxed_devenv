"""`new`, `ls`, `start`, `stop`, `rm`, `clone`, `resources`, `ssh`, `serial-console` against a fake driver and a fake backend."""

import json
import pathlib
import subprocess

import pytest
import yaml

from sandboxed_devenv import (
    cli,
    common,
    config,
    control_backends,
    control_creds,
    control_driver,
    control_images,
    control_inventory,
    remote,
)

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test"
HOST_KEYS = ["ssh-ed25519 AAAAhost", "ecdsa-sha2-nistp256 AAAAecdsa"]


class FakeDriver:
    name = "utm"

    def __init__(self, runner=None):
        self.runner = runner or common.Runner()
        self.vms = {}
        self.specs = {}
        self.seeds = {}
        self.calls = []
        self.live_resources = {}

    def available(self):
        retval = None
        return retval

    def exists(self, name):
        retval = name in self.vms
        return retval

    def create(self, spec):
        self.calls.append(("create", spec.name))
        self.specs[spec.name] = spec
        self.seeds[spec.name] = {path.name: path.read_text() for path in spec.seed_dir.iterdir()}
        self.vms[spec.name] = control_driver.STOPPED
        self.live_resources[spec.name] = (spec.cpus, spec.memory_gib)

    def start(self, name):
        self.calls.append(("start", name))
        self.vms[name] = control_driver.STARTED

    def stop(self, name, force=False):
        self.calls.append(("stop", name, force))
        self.vms[name] = control_driver.STOPPED

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.vms[name]

    def clone(self, name, new_name):
        self.vms[new_name] = control_driver.STOPPED

    def list(self):
        retlist = []
        for name, state in self.vms.items():
            retlist.append(control_driver.VmInfo(name=name, state=state))
        return retlist

    def state(self, name):
        retval = self.vms[name]
        return retval

    def address(self, name):
        retval = None
        return retval

    def console_command(self, name):
        retlist = ["screen", "/dev/ttys009"]
        return retlist

    def export_disk(self, name, dest):
        self.calls.append(("export", name))
        pathlib.Path(dest).write_bytes(b"disk")

    def resources(self, name):
        retval = self.live_resources.get(name)
        return retval

    def set_resources(self, name, cpus, memory_gib):
        self.calls.append(("set_resources", name, cpus, memory_gib))
        current = self.live_resources[name]
        self.live_resources[name] = (cpus or current[0], memory_gib or current[1])


class FakeBackend:
    """What remote.backend() returns: a ready backend whose status lists the repositories asked for."""

    def __init__(self, address, vm_name, runner=None):
        self.address = address
        self.vm_name = vm_name
        self.runner = runner or common.Runner()
        self.target = f"dev@{address}"
        self.calls = []
        self.repositories = []

    def wait_for_ssh(self, timeout, interval=5.0):
        self.calls.append("wait_for_ssh")
        retval = 1.0
        return retval

    def status(self):
        retdict = {
            "hostname": self.vm_name, "ready": True, "package_version": "0.1.0", "ssh_host_keys": list(HOST_KEYS),
            "repositories": [
                {"repository": name, "fingerprint": f"SHA256:{name}", "public_key": f"ssh-ed25519 KEY{name}", "checkout": None}
                for name in self.repositories
            ],
        }
        return retdict

    def wait_ready(self, timeout, interval=10.0):
        self.calls.append("wait_ready")
        retval = self.status()
        return retval

    def tool(self, args, sudo=False, parse_json=False, check=True, json_flag=True):
        self.calls.append(("tool", tuple(args)))
        if args[:3] == ["backend", "repo", "rekey"]:
            assert json_flag is False, "the backend repo commands take no --json flag"
            retval = {"repository": args[3], "fingerprint": "SHA256:new", "public_key": "ssh-ed25519 NEW", "created": True}
        else:
            retval = {}
        return retval

    def ssh_command(self, remote_command=None, tty=False):
        retlist = ["ssh", self.target]
        if remote_command:
            retlist.append(" ".join(remote_command))
        return retlist


class FakeClient:
    def __init__(self):
        self.calls = []
        self.target = "jsw@192.168.64.10"
        self.fail = False

    def tool(self, args, sudo=False, parse_json=False, check=True):
        self.calls.append((tuple(args), sudo))
        if self.fail:
            raise remote.RemoteError("boom")
        retval = subprocess.CompletedProcess(args, 0, "", "")
        return retval


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A state directory with a base image for this package version, the rig key, a config
    naming desk as the client host, and the driver, backend and client host replaced by fakes."""
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "gw_ed25519").write_text("private")
    (tmp_path / "secrets" / "gw_ed25519.pub").write_text(PUBKEY + "\n")
    (tmp_path / "config.yaml").write_text(
        "client_host: ssh://jsw@192.168.64.10\ngit:\n  user_name: Dev\n  user_email: dev@example.invalid\n")
    images = config.images_dir()
    images.mkdir()
    name = control_images.image_name(common.package_version())
    control_images.disk_path(name).write_bytes(b"\0" * 4096)
    record = control_images.ImageRecord(name=name, driver="utm", architecture="aarch64", package_version=common.package_version(),
                                        ide_backend_version="2026.2.3", ubuntu_release="26.04", ubuntu_image_serial="20260927",
                                        cloudinit_sha256="0" * 64, disk_gib=32)
    control_images.record_path(name).write_text(record.to_yaml())
    driver = FakeDriver()
    backends = {}
    client = FakeClient()

    def fake_backend(address, vm_name, runner=None):
        if address not in backends:
            backends[address] = FakeBackend(address, vm_name, runner)
        retval = backends[address]
        return retval

    monkeypatch.setattr(control_driver, "load_driver", lambda runner=None: driver)
    monkeypatch.setattr(remote, "backend", fake_backend)
    monkeypatch.setattr(remote, "client_host", lambda user_config, runner=None: client)
    monkeypatch.setattr(control_inventory, "address_answers", lambda address, timeout=1.0: False)
    monkeypatch.setattr(control_creds, "ensure_rig_key", lambda runner=None: tmp_path / "secrets" / "gw_ed25519")
    monkeypatch.setattr("sys.stdin", _Tty())
    retval = {"home": tmp_path, "driver": driver, "backends": backends, "client": client, "image": name}
    return retval


class _Tty:
    def isatty(self):
        retval = False
        return retval


def test_hostname_for():
    assert control_backends.hostname_for("idesbx-ide_sandbox") == "idesbx-ide-sandbox"
    assert control_backends.hostname_for("x.y") == "x-y"
    assert control_backends.hostname_for("___") == "backend"


def test_new_dry_run_prints_the_plan(rig, capsys):
    status = cli.main(["--dry-run", "new", "m2test", "--repo", "jeffsw/a", "--cpus", "2"])
    assert status == 0
    out = capsys.readouterr().out
    assert "would launch idesbx-m2test for project m2test" in out
    assert "2 CPUs and 8 GiB at 192.168.64.20" in out
    assert "repositories jeffsw/a" in out
    assert "connect add m2test 192.168.64.20" in out
    assert control_inventory.load("m2test") is None
    assert rig["driver"].calls == []


def test_new_launches_records_and_connects(rig, capsys):
    status = cli.main(["new", "m2test", "--repo", "jeffsw/a", "--repo", "jeffsw/b", "--deploy-key-grants-repo-write"])
    assert status == 0
    out = capsys.readouterr().out
    driver = rig["driver"]
    assert driver.calls == [("create", "idesbx-m2test"), ("start", "idesbx-m2test")]
    spec = driver.specs["idesbx-m2test"]
    assert spec.disk == control_images.disk_path(rig["image"])
    assert (spec.cpus, spec.memory_gib) == (4, 8)
    seed = driver.seeds["idesbx-m2test"]
    assert set(seed) == {"user-data", "meta-data", "network-config"}
    user_data = yaml.safe_load(seed["user-data"])
    assert user_data["hostname"] == "idesbx-m2test"
    assert PUBKEY in seed["user-data"]
    assert "192.168.64.20/24" in seed["network-config"]
    assert seed["meta-data"].startswith("instance-id: idesbx-m2test-")
    backend = rig["backends"]["192.168.64.20"]
    assert backend.calls == ["wait_for_ssh", "wait_ready"]
    record = control_inventory.require("m2test")
    assert record.address == "192.168.64.20"
    assert record.vm_name == "idesbx-m2test"
    assert record.image == rig["image"]
    assert [(r.name, r.access) for r in record.repos] == [("jeffsw/a", "write"), ("jeffsw/b", "write")]
    assert record.repos[0].fingerprint is None  # the fake backend lists only what it was told about
    assert rig["client"].calls == [(("connect", "add", "m2test", "192.168.64.20", *HOST_KEYS), True)]
    assert "m2test ready: idesbx-m2test at 192.168.64.20" in out
    assert "2 deploy key(s) to register by hand" in out
    assert "gh repo deploy-key add idesbx-m2test-jeffsw-a.pub --repo jeffsw/a --title idesbx-m2test --allow-write" in out
    assert "sandboxed-devenv ssh m2test -- cat /home/dev/.ssh/deploy_jeffsw_b.pub > idesbx-m2test-jeffsw-b.pub" in out
    assert "backend repo add jeffsw/b" in out
    assert not list(config.images_dir().glob(".new-*"))
    assert cli.main(["new", "m2test"]) == 1
    assert "already in the inventory" in capsys.readouterr().err
    assert cli.main(["new", "bad name"]) == 1
    assert cli.main(["new", "m2test2", "--memory-gib", "4"]) == 0
    assert control_inventory.require("m2test2").address == "192.168.64.21"


def test_new_without_an_image_or_with_a_stray_vm(rig, capsys):
    control_images.disk_path(rig["image"]).unlink()
    assert cli.main(["new", "p"]) == 1
    assert "image build" in capsys.readouterr().err
    control_images.disk_path(rig["image"]).write_bytes(b"")
    rig["driver"].vms["idesbx-p"] = control_driver.STOPPED
    assert cli.main(["new", "p"]) == 1
    assert "not in the inventory" in capsys.readouterr().err


def test_new_survives_a_failed_connect_and_reports_it(rig, capsys):
    rig["client"].fail = True
    assert cli.main(["new", "p"]) == 0
    err = capsys.readouterr().err
    assert "on the client host" in err
    assert "run there by hand" in err
    assert control_inventory.load("p") is not None


def test_ls_start_stop_and_json(rig, capsys):
    assert cli.main(["ls"]) == 0
    assert "no projects" in capsys.readouterr().out
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert "PROJECT" in out and "p " in out and "started" in out and "192.168.64.20" in out
    assert cli.main(["ls", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["project"] == "p" and rows[0]["state"] == "started" and rows[0]["repos"] == ["jeffsw/a"]
    assert cli.main(["stop", "p"]) == 0
    assert rig["driver"].vms["idesbx-p"] == control_driver.STOPPED
    assert cli.main(["stop", "p"]) == 0
    assert "already stopped" in capsys.readouterr().out
    assert cli.main(["start", "p"]) == 0
    assert rig["driver"].vms["idesbx-p"] == control_driver.STARTED
    assert cli.main(["start", "p"]) == 0
    assert "already started" in capsys.readouterr().out
    assert cli.main(["stop", "p", "--force"]) == 0
    assert ("stop", "idesbx-p", True) in rig["driver"].calls
    assert cli.main(["start", "nope"]) == 1
    assert "no project named nope" in capsys.readouterr().err


def test_rm_needs_yes_without_a_terminal_then_removes_everything(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["rm", "p"]) == 1
    assert "refusing without --yes" in capsys.readouterr().err
    assert control_inventory.load("p") is not None
    assert cli.main(["--dry-run", "rm", "p"]) == 0
    assert "would stop idesbx-p" in capsys.readouterr().out
    assert cli.main(["rm", "p", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "p removed" in out
    assert "idesbx-p" not in rig["driver"].vms
    assert control_inventory.load("p") is None
    assert rig["client"].calls[-1] == (("connect", "rm", "p"), True)
    assert not remote.known_hosts_file("idesbx-p").exists()


def test_clone_stops_copies_rekeys_and_marks_disposable(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "clone", "p", "q"]) == 0
    out = capsys.readouterr().out
    assert "would stop idesbx-p (started)" in out
    assert "fresh key pair" in out
    assert cli.main(["clone", "p", "q"]) == 1
    assert "refusing without --yes" in capsys.readouterr().err
    assert cli.main(["clone", "p", "q", "--yes"]) == 0
    out = capsys.readouterr().out
    driver = rig["driver"]
    calls = driver.calls
    assert ("stop", "idesbx-p", False) in calls
    assert ("export", "idesbx-p") in calls
    assert calls.index(("export", "idesbx-p")) < calls.index(("create", "idesbx-q"))
    assert calls.count(("start", "idesbx-p")) == 2
    assert driver.vms["idesbx-q"] == control_driver.STARTED
    record = control_inventory.require("q")
    assert record.disposable is True
    assert record.cloned_from == "p"
    assert record.address == "192.168.64.21"
    assert record.repos[0].name == "jeffsw/a"
    clone_backend = rig["backends"]["192.168.64.21"]
    assert ("tool", ("backend", "repo", "rekey", "jeffsw/a")) in clone_backend.calls
    assert "disposable copy of p with 1 fresh deploy keys" in out
    assert cli.main(["ls"]) == 0
    assert "disposable from p" in capsys.readouterr().out
    assert not list(config.images_dir().glob(".new-*"))


def test_resources_shows_and_changes_with_a_stop(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["resources", "p"]) == 0
    assert "4 CPUs, 8 GiB; inventory says 4 CPUs, 8 GiB" in capsys.readouterr().out
    assert cli.main(["--dry-run", "resources", "p", "--cpus", "2"]) == 0
    out = capsys.readouterr().out
    assert "would stop idesbx-p (started)" in out and "would set idesbx-p to 2 CPUs and 8 GiB" in out
    assert cli.main(["resources", "p", "--cpus", "2"]) == 1
    assert cli.main(["resources", "p", "--cpus", "2", "--memory-gib", "6", "--yes"]) == 0
    assert "now has 2 CPUs and 6 GiB and is starting again" in capsys.readouterr().out
    driver = rig["driver"]
    assert ("set_resources", "idesbx-p", 2, 6) in driver.calls
    assert driver.vms["idesbx-p"] == control_driver.STARTED
    record = control_inventory.require("p")
    assert (record.cpus, record.memory_gib) == (2, 6)
    assert cli.main(["stop", "p"]) == 0
    assert cli.main(["resources", "p", "--memory-gib", "4"]) == 0
    assert driver.vms["idesbx-p"] == control_driver.STOPPED


def test_ssh_and_serial_console_dry_runs(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "ssh", "p"]) == 0
    assert "would run: ssh dev@192.168.64.20" in capsys.readouterr().out
    assert cli.main(["--dry-run", "ssh", "p", "--", "hostname", "-f"]) == 0
    assert capsys.readouterr().out.strip().endswith("hostname -f")
    assert cli.main(["--dry-run", "serial-console", "p"]) == 0
    assert "would run: screen /dev/ttys009" in capsys.readouterr().out
    assert cli.main(["ssh", "nope"]) == 1


def test_git_identity_falls_back_to_the_global_config(rig, monkeypatch):
    class Git:
        dry_run = False
        verbose = False

        def run(self, command, **kwargs):
            value = {"user.name": "Someone", "user.email": "someone@example.invalid"}[command[-1]]
            retval = subprocess.CompletedProcess(command, 0, value + "\n", "")
            return retval

    identity = control_backends.git_identity(config.UserConfig(), Git())
    assert (identity.user_name, identity.user_email) == ("Someone", "someone@example.invalid")

    class NoGit:
        dry_run = False
        verbose = False

        def run(self, command, **kwargs):
            retval = subprocess.CompletedProcess(command, 1, "", "")
            return retval

    with pytest.raises(remote.RemoteError):
        control_backends.git_identity(config.UserConfig(), NoGit())
    configured = config.UserConfig(git=config.GitIdentity(user_name="A", user_email="a@b"))
    assert control_backends.git_identity(configured, NoGit()).user_name == "A"
