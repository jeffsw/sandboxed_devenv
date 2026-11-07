"""`client start/session/exec`: the commands they build, checked without a client host."""

import pathlib

from sandboxed_devenv import cli, host_client


def test_sudo_command_sets_the_display_environment():
    sock = pathlib.Path("/run/user/1000/wayland-0")
    command = host_client.sudo_command(sock, "1001", ["true"], xauthority=False)
    assert command[:4] == ["sudo", "--user=gw", "env", "--unset=XAUTHORITY"]
    assert "WAYLAND_DISPLAY=/run/user/1000/wayland-0" in command
    assert "XDG_RUNTIME_DIR=/run/user/1001" in command
    assert "DISPLAY=:9" in command
    assert "HOME=/home/gw" in command
    assert not [part for part in command if part.startswith("XAUTHORITY=")]
    assert command[-1] == "true"
    # `sudo --login` would re-parse the line through bash -c (gotcha of 9 October 2026).
    assert "--login" not in command and "-i" not in command


def test_exec_passes_the_cookie():
    command = host_client.sudo_command(pathlib.Path("/run/user/1000/wayland-0"), "1001", ["canary"], xauthority=True)
    assert "XAUTHORITY=/home/gw/.Xauthority-gw" in command
    assert command[-1] == "canary"


def test_lend_commands_open_the_socket_and_close_every_other_entry(tmp_path):
    sock = tmp_path / "wayland-0"
    for name in ("wayland-0", "bus", "pipewire-0", ".flatpak-helper"):
        (tmp_path / name).touch()
    (tmp_path / "systemd").mkdir()
    commands = host_client.lend_commands(tmp_path, sock)
    assert commands[0] == ["setfacl", "--modify", "u:gw:x", str(tmp_path)]
    assert commands[1] == ["setfacl", "--modify", "u:gw:rw", str(sock)]
    closed = set()
    for command in commands[2:]:
        assert command[:3] == ["setfacl", "--modify", "u:gw:-"]
        assert host_client.is_closing_entry(command)
        closed.add(pathlib.Path(command[3]).name)
    assert closed == {"bus", "pipewire-0", ".flatpak-helper", "systemd"}


def test_wayland_socket_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert host_client.wayland_socket() == (tmp_path, tmp_path / "wayland-0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert host_client.wayland_socket()[1] == tmp_path / "wayland-1"
    monkeypatch.setenv("WAYLAND_DISPLAY", "/elsewhere/wayland-0")
    assert host_client.wayland_socket()[1] == pathlib.Path("/elsewhere/wayland-0")


def test_without_separator():
    assert host_client.without_separator(["--", "a", "--", "b"]) == ["a", "--", "b"]
    assert host_client.without_separator(["a"]) == ["a"]
    assert host_client.without_separator([]) == []


def test_client_start_dry_run_prints_the_plan(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    (tmp_path / "wayland-0").touch()
    (tmp_path / "bus").touch()
    status = cli.main(["--dry-run", "client", "start", "--", "--help"])
    assert status == 0
    out = capsys.readouterr().out
    assert f"setfacl --modify u:gw:rw {tmp_path}/wayland-0" in out
    assert f"setfacl --modify u:gw:- {tmp_path}/bus" in out
    assert "client session --help" in out
    assert "sudo --user=gw env --unset=XAUTHORITY" in out


def test_client_exec_dry_run_and_the_missing_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "wayland-0").touch()
    status = cli.main(["--dry-run", "client", "exec", "--", "sandboxed-devenv", "canary", "--role", "client"])
    assert status == 0
    out = capsys.readouterr().out
    assert "XAUTHORITY=/home/gw/.Xauthority-gw sandboxed-devenv canary --role client" in out
    assert cli.main(["--dry-run", "client", "exec"]) == 2


def test_client_session_dry_run_names_xwayland_and_the_launcher(capsys):
    status = cli.main(["--dry-run", "client", "session"])
    assert status == 0
    out = capsys.readouterr().out
    assert "Xwayland :9 -rootless -auth /home/gw/.Xauthority-gw" in out
    assert "/home/gw/gateway/bin/gateway.sh" in out
