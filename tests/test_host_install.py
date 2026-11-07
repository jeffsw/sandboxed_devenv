"""`host install`: the file sync it is built on, and what it prints where it cannot run."""

import grp
import os
import pwd

from sandboxed_devenv import cli, host_install

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def test_sync_file_creates_then_reports_unchanged(tmp_path):
    dest = tmp_path / "etc" / "thing.conf"
    first = host_install.sync_file(dest, b"one\n", ME, MY_GROUP, 0o600, apply=True)
    assert first.status == host_install.CHANGED
    assert first.detail == "created"
    assert dest.read_bytes() == b"one\n"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    again = host_install.sync_file(dest, b"one\n", ME, MY_GROUP, 0o600, apply=True)
    assert again.status == host_install.UNCHANGED


def test_sync_file_reports_what_would_change_and_then_changes_it(tmp_path):
    dest = tmp_path / "thing.conf"
    dest.write_bytes(b"old\n")
    dest.chmod(0o644)
    check = host_install.sync_file(dest, b"new\n", ME, MY_GROUP, 0o600, apply=False)
    assert check.status == host_install.WOULD_CHANGE
    assert check.detail == "content, mode"
    assert dest.read_bytes() == b"old\n"
    applied = host_install.sync_file(dest, b"new\n", ME, MY_GROUP, 0o600, apply=True)
    assert applied.status == host_install.CHANGED
    assert dest.read_bytes() == b"new\n"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".*.tmp"))


def test_sync_file_makes_parents_with_ssh_closed(tmp_path):
    dest = tmp_path / "home" / "gw" / ".ssh" / "config"
    host_install.sync_file(dest, b"Host *\n", ME, MY_GROUP, 0o600, apply=True)
    assert oct((tmp_path / "home" / "gw" / ".ssh").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "home" / "gw").stat().st_mode & 0o777) == "0o755"


def test_sync_file_with_an_unknown_owner(tmp_path):
    dest = tmp_path / "x"
    check = host_install.sync_file(dest, b"", "no-such-user-here", MY_GROUP, 0o644, apply=False)
    assert check.status == host_install.WOULD_CHANGE
    applied = host_install.sync_file(dest, b"", "no-such-user-here", MY_GROUP, 0o644, apply=True)
    assert applied.status == host_install.FAILED


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
