"""`new`, `ls`, `start`, `stop`, `rm`, `clone`, `resources`, `ssh`, `serial-console` against the fakes in conftest."""

import json
import subprocess

import pytest
import yaml

from sandboxed_devenv import cli, config, control_backends, control_driver, control_images, control_inventory, remote


def test_hostname_for():
    assert control_backends.hostname_for("idesbx-ide_sandbox") == "idesbx-ide-sandbox"
    assert control_backends.hostname_for("x.y") == "x-y"
    assert control_backends.hostname_for("___") == "backend"


def test_new_dry_run_prints_the_plan(rig, capsys):
    status = cli.main(["--dry-run", "new", "m2test", "--repo", "jeffsw/a", "--cpus", "2"])
    assert status == 0
    out = capsys.readouterr().out
    assert "would launch idesbx-m2test for project m2test" in out
    assert "would mint the VM's SSH host key" in out
    assert "2 CPUs and 8 GiB at 192.168.64.20" in out
    assert "repositories jeffsw/a" in out
    assert "connect add m2test 192.168.64.20" in out
    assert "register each repository's minted deploy key" in out
    assert control_inventory.load("m2test") is None
    assert rig["driver"].calls == []


def test_new_launches_records_registers_and_connects(rig, capsys):
    status = cli.main(["new", "m2test", "--repo", "jeffsw/a", "--repo", "jeffsw/b", "--deploy-key-grants-repo-write"])
    assert status == 0
    out = capsys.readouterr().out
    driver = rig["driver"]
    assert driver.calls == [("create", "idesbx-m2test"), ("start", "idesbx-m2test")]
    spec = driver.specs["idesbx-m2test"]
    assert spec.image == rig["image"] and spec.disk is None
    assert (spec.cpus, spec.memory_gib) == (4, 8)
    assert spec.address is None and spec.disk_gib == 32
    seed = driver.seeds["idesbx-m2test"]
    assert set(seed) == {"user-data", "meta-data", "network-config"}
    user_data = yaml.safe_load(seed["user-data"])
    assert user_data["hostname"] == "idesbx-m2test"
    assert user_data["users"][0]["ssh_authorized_keys"] == [rig["pubkey"], rig["sandbox_pubkey"]]
    assert user_data["ssh_keys"]["ed25519_public"] == rig["host_key"][1]
    assert user_data["ssh_keys"]["ed25519_private"] == rig["host_key"][0]
    assert user_data["ssh_genkeytypes"] == ["ed25519"]
    pinned = driver.pinned_at_start["idesbx-m2test"]
    assert pinned == f"192.168.64.20 {' '.join(rig['host_key'][1].split()[:2])}\n"
    assert remote.known_hosts_file("idesbx-m2test").read_text() == pinned
    assert control_inventory.require("m2test").ssh_host_key == rig["host_key"][1]
    assert "192.168.64.20/24" in seed["network-config"]
    assert seed["meta-data"].startswith("instance-id: idesbx-m2test-")
    backend = rig["backends"]["192.168.64.20"]
    assert backend.calls[:2] == ["wait_for_ssh", "wait_ready"]
    assert ("tool", ("backend", "repo", "add", "jeffsw/a")) in backend.calls
    github = rig["github"]
    assert ("add", "jeffsw/a", "sandboxed-devenv m2test", False) in github.calls
    assert ("add", "jeffsw/b", "sandboxed-devenv m2test", False) in github.calls
    record = control_inventory.require("m2test")
    assert record.address == "192.168.64.20"
    assert record.vm_name == "idesbx-m2test"
    assert record.image == rig["image"]
    assert [(r.name, r.access, r.state) for r in record.repos] == [("jeffsw/a", "write", "active"), ("jeffsw/b", "write", "active")]
    assert record.repos[0].github_key_id == 1001
    assert record.repos[0].fingerprint == "SHA256:jeffsw/a"
    assert record.repos[0].checkout.startswith("/home/dev/proj/")
    assert record.repos[0].registered is not None
    assert rig["client"].calls == [(("connect", "add", "m2test", "192.168.64.20", rig["host_key"][1]), True)]
    assert "m2test ready: idesbx-m2test at 192.168.64.20" in out
    assert "deploy key registered on GitHub as id 1001 (write)" in out
    assert "cloned to /home/dev/proj/" in out
    assert "by hand" not in out
    assert not list(config.images_dir().glob(".new-*"))
    assert cli.main(["new", "m2test"]) == 1
    assert "already in the inventory" in capsys.readouterr().err
    assert cli.main(["new", "bad name"]) == 1
    assert cli.main(["new", "m2test2", "--memory-gib", "4"]) == 0
    assert control_inventory.require("m2test2").address == "192.168.64.21"


def test_new_records_the_token_when_the_secrets_dir_has_one(rig):
    (rig["home"] / "secrets" / "claude_oauth_token").write_text("tok-123\n")
    assert cli.main(["new", "p"]) == 0
    record = control_inventory.require("p")
    assert [c.kind for c in record.credentials] == ["claude-token"]
    assert record.credentials[0].fingerprint.startswith("sha256:")
    assert "tok-123" not in record.to_json()


def test_new_leaves_keys_unregistered_when_github_fails(rig, capsys):
    rig["github"].fail = True
    status = cli.main(["new", "p", "--repo", "jeffsw/a"])
    assert status == 1
    captured = capsys.readouterr()
    assert "registration failed" in captured.err
    assert "1 deploy key(s) not registered" in captured.out
    assert "gh repo deploy-key add idesbx-p-jeffsw-a.pub --repo jeffsw/a --title idesbx-p" in captured.out
    record = control_inventory.require("p")
    assert record.repos[0].state == "unregistered"
    assert record.repos[0].checkout is None
    assert rig["driver"].vms["idesbx-p"] == control_driver.STARTED


def test_new_with_repositories_fails_early_when_gh_is_logged_out(rig, capsys):
    rig["github"].logged_in = False
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 1
    assert "gh auth login" in capsys.readouterr().err
    assert rig["driver"].calls == []
    assert control_inventory.load("p") is None
    assert cli.main(["new", "p"]) == 0


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
    assert rows[0]["pending_revocations"] == 0
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


def test_rm_needs_yes_without_a_terminal_then_revokes_and_removes_everything(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    assert cli.main(["rm", "p"]) == 1
    assert "refusing without --yes" in capsys.readouterr().err
    assert control_inventory.load("p") is not None
    assert cli.main(["--dry-run", "rm", "p"]) == 0
    out = capsys.readouterr().out
    assert "would revoke the deploy keys of jeffsw/a" in out
    assert "would stop idesbx-p" in out
    assert cli.main(["rm", "p", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "deploy key id 1001 revoked on GitHub" in out
    assert "p removed" in out
    assert ("delete", "jeffsw/a", 1001) in rig["github"].calls
    assert rig["github"].keys["jeffsw/a"] == []
    assert "idesbx-p" not in rig["driver"].vms
    assert control_inventory.load("p") is None
    assert rig["client"].calls[-1] == (("connect", "rm", "p"), True)
    assert not remote.known_hosts_file("idesbx-p").exists()


def test_rm_keeps_the_record_when_a_revocation_fails(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    rig["github"].fail = True
    assert cli.main(["rm", "p", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "1 deploy key revocation(s) failed" in out
    assert "idesbx-p" not in rig["driver"].vms
    record = control_inventory.require("p")
    assert record.removed is not None
    assert record.repos[0].state == "pending-revoke"
    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert "removed" in out and "1 pending revocation(s)" in out
    assert cli.main(["start", "p"]) == 1
    assert cli.main(["new", "p"]) == 1
    assert "already in the inventory" in capsys.readouterr().err


def test_clone_stops_copies_rekeys_registers_and_marks_disposable(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "clone", "p", "q"]) == 0
    out = capsys.readouterr().out
    assert "would stop idesbx-p (started)" in out
    assert "fresh key pair" in out and "q's name" in out
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
    assert record.repos[0].state == "active"
    assert record.repos[0].fingerprint == "SHA256:jeffsw/a-g1"
    assert record.repos[0].github_key_id == 1002
    assert ("add", "jeffsw/a", "sandboxed-devenv q", True) in rig["github"].calls
    source = control_inventory.require("p")
    assert source.repos[0].github_key_id == 1001
    assert len(rig["github"].keys["jeffsw/a"]) == 2
    clone_backend = rig["backends"]["192.168.64.21"]
    assert ("tool", ("backend", "repo", "rekey", "jeffsw/a")) in clone_backend.calls
    assert ("tool", ("backend", "repo", "add", "jeffsw/a")) in clone_backend.calls
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


def test_git_identity_falls_back_to_the_global_config(rig):
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
