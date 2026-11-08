"""`client-host new / rm / ssh`: the plan for desk, and what refuses off the Mac rig."""

import pytest

from sandboxed_devenv import cli, common, control_driver


class FakeDriver:
    name = "utm"
    runner = common.Runner(dry_run=True)

    def __init__(self):
        self.vms = {}

    def available(self):
        retval = None
        return retval

    def exists(self, name):
        retval = name in self.vms
        return retval

    def state(self, name):
        retval = self.vms[name]
        return retval


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "gw_ed25519").write_text("private")
    (tmp_path / "secrets" / "gw_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    (tmp_path / "config.yaml").write_text("client_host: ssh://jsw@192.168.64.10\n")
    driver = FakeDriver()
    monkeypatch.setattr(control_driver, "load_driver", lambda runner=None: driver)
    retval = driver
    return retval


def test_new_dry_run_prints_the_plan(rig, capsys):
    assert cli.main(["--dry-run", "client-host", "new", "--cpus", "2"]) == 0
    out = capsys.readouterr().out
    assert "create VM idesbx-desk with 2 CPUs, 8 GiB, a display" in out
    assert "hostname desk, user jsw, address 192.168.64.10" in out
    assert "routes to 192.168.64.0/25 and 192.168.64.128/25 via 192.168.64.1" in out
    assert "sudo sandboxed-devenv host install" in out
    assert "would stop and delete" not in out


def test_new_refuses_an_existing_desk_unless_replaced(rig, capsys):
    rig.vms["idesbx-desk"] = control_driver.STARTED
    assert cli.main(["--dry-run", "client-host", "new"]) == 1
    assert "--replace" in capsys.readouterr().err
    assert cli.main(["--dry-run", "client-host", "new", "--replace"]) == 0
    assert "would stop and delete the existing idesbx-desk" in capsys.readouterr().out


def test_commands_refuse_when_this_machine_is_the_client_host(rig, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("client_host: local\n")
    assert cli.main(["--dry-run", "client-host", "new"]) == 1
    assert "host install" in capsys.readouterr().err
    assert cli.main(["--dry-run", "client-host", "ssh"]) == 1


def test_new_wants_the_two_addresses_to_agree(rig, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("client_host: ssh://jsw@192.168.64.11\n")
    assert cli.main(["--dry-run", "client-host", "new"]) == 1
    assert "utm.client_host_address" in capsys.readouterr().err


def test_rm_and_ssh_dry_runs(rig, capsys):
    assert cli.main(["--dry-run", "client-host", "rm"]) == 0
    assert "no VM named idesbx-desk" in capsys.readouterr().out
    rig.vms["idesbx-desk"] = control_driver.STOPPED
    assert cli.main(["--dry-run", "client-host", "rm"]) == 0
    assert "would stop idesbx-desk (stopped) if needed and delete it" in capsys.readouterr().out
    assert cli.main(["--dry-run", "client-host", "ssh", "--", "cloud-init", "status"]) == 0
    out = capsys.readouterr().out
    assert "jsw@192.168.64.10" in out
    assert out.strip().endswith("cloud-init status")
