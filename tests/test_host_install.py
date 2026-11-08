"""`host install`: the file sync it is built on, and what it prints where it cannot run."""

import grp
import os
import pwd

from sandboxed_devenv import cli, host_install, steps

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def test_sync_file_creates_then_reports_unchanged(tmp_path):
    dest = tmp_path / "etc" / "thing.conf"
    first = steps.sync_file(dest, b"one\n", ME, MY_GROUP, 0o600, apply=True)
    assert first.status == steps.CHANGED
    assert first.detail == "created"
    assert dest.read_bytes() == b"one\n"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    again = steps.sync_file(dest, b"one\n", ME, MY_GROUP, 0o600, apply=True)
    assert again.status == steps.UNCHANGED


def test_sync_file_reports_what_would_change_and_then_changes_it(tmp_path):
    dest = tmp_path / "thing.conf"
    dest.write_bytes(b"old\n")
    dest.chmod(0o644)
    check = steps.sync_file(dest, b"new\n", ME, MY_GROUP, 0o600, apply=False)
    assert check.status == steps.WOULD_CHANGE
    assert check.detail == "content, mode"
    assert dest.read_bytes() == b"old\n"
    applied = steps.sync_file(dest, b"new\n", ME, MY_GROUP, 0o600, apply=True)
    assert applied.status == steps.CHANGED
    assert dest.read_bytes() == b"new\n"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".*.tmp"))


def test_sync_file_makes_parents_with_ssh_closed(tmp_path):
    dest = tmp_path / "home" / "gw" / ".ssh" / "config"
    steps.sync_file(dest, b"Host *\n", ME, MY_GROUP, 0o600, apply=True)
    assert oct((tmp_path / "home" / "gw" / ".ssh").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "home" / "gw").stat().st_mode & 0o777) == "0o755"


def test_sync_file_with_an_unknown_owner(tmp_path):
    dest = tmp_path / "x"
    check = steps.sync_file(dest, b"", "no-such-user-here", MY_GROUP, 0o644, apply=False)
    assert check.status == steps.WOULD_CHANGE
    applied = steps.sync_file(dest, b"", "no-such-user-here", MY_GROUP, 0o644, apply=True)
    assert applied.status == steps.FAILED


def test_dry_run_without_root_prints_the_plan(capsys):
    status = cli.main(["--dry-run", "host", "install"])
    assert status == 0
    out = capsys.readouterr().out
    for fragment in ("packages:", "user gw:", "linger:", "file /etc/nftables.conf", "file /etc/apparmor.d/jetbrains-client",
                     "file /home/gw/.config/JetBrains/JetBrainsGateway2026.2/options/sshConfigs.xml", "gw key pair:",
                     "apparmor:", "sysctl:", "nftables:", "gateway: JetBrainsGateway-2026.2.2"):
        assert fragment in out, fragment


def test_check_needs_a_linux_client_host_or_root(capsys):
    status = cli.main(["host", "install", "--check"])
    assert status == 1
    err = capsys.readouterr().err
    assert "Linux" in err or "needs root" in err


def test_host_check_dry_run_runs_the_canary_as_gw(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "wayland-0").touch()
    status = cli.main(["--dry-run", "host", "check"])
    assert status == 0
    out = capsys.readouterr().out
    assert "sudo --user=gw env" in out
    assert "canary --role client" in out


def test_gateway_url_names_the_version():
    assert "JetBrainsGateway-2026.2.2" in host_install.gateway_url("2026.2.2")
    assert host_install.gateway_url("2026.2.2").endswith(".tar.gz")


def test_a_kept_file_is_written_once_and_then_left_alone(tmp_path, monkeypatch):
    from sandboxed_devenv import render

    entry = render.ManifestEntry(dest=str(tmp_path / "kept.xml"), owner=ME, group=MY_GROUP, mode="0600", template=False,
                                 keep=True, description="kept")
    monkeypatch.setattr(render, "load_manifest", lambda: {"host": {"kept.xml": entry}})
    monkeypatch.setattr(render, "render_entry", lambda role, name, entry, values: "<fresh/>")
    ctx = host_install.Context(apply=True, values={}, command="host install", secrets_dir=tmp_path)
    first = host_install.step_files(ctx)
    assert [result.status for result in first] == [steps.CHANGED]
    (tmp_path / "kept.xml").write_text("<edited/>")
    second = host_install.step_files(ctx)
    assert [result.status for result in second] == [steps.UNCHANGED]
    assert "kept" in second[0].detail
    assert (tmp_path / "kept.xml").read_text() == "<edited/>"


def test_host_check_dry_run_passes_json_and_expect_to_the_canary(capsys, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert cli.main(["--dry-run", "host", "check", "--json", "--expect"]) == 0
    out = capsys.readouterr().out
    assert "canary --role client --json --expect" in out
    assert cli.main(["--dry-run", "host", "check", "--expect", "/tmp/golden.json"]) == 0
    assert "--expect /tmp/golden.json" in capsys.readouterr().out


def test_incus_pool_step_creates_the_dir_pool_where_incus_exists(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.setattr(host_install.shutil, "which", lambda name: None)
    ctx = host_install.Context(apply=True, values={}, command="host install", secrets_dir=tmp_path)
    assert host_install.step_incus_pool(ctx) == []
    monkeypatch.setattr(host_install.shutil, "which", lambda name: "/usr/bin/incus")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        status = 1 if command[:3] == ["incus", "storage", "show"] else 0
        retval = subprocess.CompletedProcess(command, status, "", "")
        return retval

    monkeypatch.setattr(host_install, "run", fake_run)
    (tmp_path / "config.yaml").write_text("incus:\n  storage_dir: /var/lib/sandboxed-devenv/incus\n")
    outcomes = host_install.step_incus_pool(ctx)
    assert [result.status for result in outcomes] == [steps.CHANGED]
    assert commands[-1] == ["incus", "storage", "create", "sandboxed-devenv", "dir", "source=/var/lib/sandboxed-devenv/incus"]
    monkeypatch.setattr(host_install, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""))
    assert [result.status for result in host_install.step_incus_pool(ctx)] == [steps.UNCHANGED]


def test_key_pair_step_mints_the_pairs_on_a_machine_that_is_its_own_control_side(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    gw_key = tmp_path / "vm_ed25519"
    monkeypatch.setattr(host_install, "GW_KEY", gw_key)
    monkeypatch.setattr(host_install, "sync_file",
                        lambda dest, content, owner, group, mode, apply: steps.sync_file(dest, content, ME, MY_GROUP, mode, apply))
    ctx = host_install.Context(apply=True, values={}, command="host install", secrets_dir=secrets)
    outcomes = host_install.step_key_pair(ctx)
    assert [result.status for result in outcomes] == [steps.CHANGED, steps.CHANGED, steps.CHANGED]
    assert outcomes[0].label == "key pairs"
    assert (secrets / "control_ed25519").is_file() and (secrets / "sandbox_ed25519.pub").is_file()
    assert gw_key.read_bytes() == (secrets / "sandbox_ed25519").read_bytes()
    again = host_install.step_key_pair(ctx)
    assert [result.status for result in again] == [steps.UNCHANGED, steps.UNCHANGED]
    (tmp_path / "config.yaml").write_text("client_host: ssh://jsw@192.168.64.10\n")
    for path in secrets.iterdir():
        path.unlink()
    assert [result.status for result in host_install.step_key_pair(ctx)] == [steps.FAILED]
