"""remote.py: the ssh and scp command lines, JSON parsing, the waits, and how machines are named."""

import json
import pathlib
import subprocess

import pytest

from sandboxed_devenv import common, config, remote


class FakeRunner:
    """Records every command; answers each with the next scripted (status, stdout) pair."""

    dry_run = False
    verbose = False

    def __init__(self, outputs=None):
        self.commands = []
        self.kwargs = []
        self.outputs = list(outputs or [])

    def run(self, command, **kwargs):
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if self.outputs:
            status, stdout = self.outputs.pop(0)
        else:
            status, stdout = 0, ""
        retval = subprocess.CompletedProcess(command, status, stdout, "")
        return retval


def make_remote(runner=None, **overrides) -> remote.Remote:
    fields = dict(user="dev", host="192.168.64.19", key=pathlib.Path("/secrets/gw_ed25519"),
                  known_hosts=pathlib.Path("/kh/idesbx-image-build"))
    fields.update(overrides)
    retval = remote.Remote(runner=runner or FakeRunner(), **fields)
    return retval


def test_ssh_command_carries_every_option_and_joins_the_remote_command():
    command = make_remote().ssh_command(["cloud-init", "status", "--wait"])
    assert command[0] == "ssh"
    assert command[1:3] == ["-F", "none"]
    assert "IdentitiesOnly=yes" in command
    assert "IdentityAgent=none" in command
    assert "ForwardAgent=no" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "UserKnownHostsFile=/kh/idesbx-image-build" in command
    assert command[command.index("-i") + 1] == "/secrets/gw_ed25519"
    assert command[command.index("-p") + 1] == "22"
    assert command[-2:] == ["dev@192.168.64.19", "cloud-init status --wait"]


def test_ssh_command_without_a_command_and_with_a_tty():
    command = make_remote(port=2222).ssh_command(tty=True)
    assert command[-1] == "dev@192.168.64.19"
    assert "-t" in command
    assert command[command.index("-p") + 1] == "2222"


def test_remote_command_is_quoted_so_the_far_shell_expands_nothing(tmp_path):
    command = make_remote().ssh_command(["ls", "~/proj", "$HOME", "*.whl"])
    assert command[-1] == "ls '~/proj' '$HOME' '*.whl'"
    # Local follows the same rule: a `~` in a destination is a directory called `~`, not a home.
    local = remote.Local(command_path="/x", runner=FakeRunner())
    with pytest.raises(FileNotFoundError):
        local.push([tmp_path / "missing.whl"], str(tmp_path / "~" / "x.whl"))


def test_scp_command():
    command = make_remote().scp_command([pathlib.Path("/b/x.whl")], "~/x.whl")
    assert command[:2] == ["scp", "-q"]
    assert command[command.index("-P") + 1] == "22"
    assert command[-2:] == ["/b/x.whl", "dev@192.168.64.19:~/x.whl"]


def test_tool_runs_the_global_command_and_parses_json():
    runner = FakeRunner([(0, json.dumps({"ready": True, "hostname": "x"}))])
    report = make_remote(runner).tool(["backend", "status"], parse_json=True)
    assert report == {"ready": True, "hostname": "x"}
    assert runner.commands[0][-1] == f"{common.GLOBAL_COMMAND_PATH} backend status --json"
    assert runner.kwargs[0]["capture_output"] is True


def test_tool_forwards_the_colour_setting_only_for_output_that_comes_back(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(common, "color_wanted", lambda stream=None: True)
    make_remote(runner).tool(["connect", "ls"])
    assert runner.commands[0][-1] == f"env {common.COLOR_ENV_VAR}=always {common.GLOBAL_COMMAND_PATH} connect ls"
    make_remote(runner).tool(["connect", "ls"], sudo=True)
    assert runner.commands[1][-1].startswith(f"sudo env {common.COLOR_ENV_VAR}=always ")
    runner = FakeRunner([(0, "{}")])
    make_remote(runner).tool(["backend", "status"], parse_json=True)
    assert not runner.commands[0][-1].startswith("env ")
    monkeypatch.setattr(common, "color_wanted", lambda stream=None: False)
    runner = FakeRunner()
    make_remote(runner).tool(["connect", "ls"])
    assert runner.commands[0][-1] == f"{common.GLOBAL_COMMAND_PATH} connect ls"


def test_tool_without_the_json_flag():
    runner = FakeRunner([(0, json.dumps({"created": True}))])
    result = make_remote(runner).tool(["backend", "repo", "rekey", "o/r"], parse_json=True, json_flag=False)
    assert result == {"created": True}
    assert runner.commands[0][-1] == f"{common.GLOBAL_COMMAND_PATH} backend repo rekey o/r"


def test_tool_with_sudo_and_bad_json():
    runner = FakeRunner([(0, "not json")])
    with pytest.raises(remote.RemoteError):
        make_remote(runner).tool(["backend", "prepare"], sudo=True, parse_json=True)
    assert runner.commands[0][-1].startswith(f"sudo {common.GLOBAL_COMMAND_PATH} backend prepare")


def test_run_raises_on_failure_unless_told_not_to():
    runner = FakeRunner([(1, ""), (1, "")])
    target = make_remote(runner)
    with pytest.raises(remote.RemoteError):
        target.run(["false"])
    assert target.run(["false"], check=False).returncode == 1


def test_install_wheel_pushes_installs_and_reports_the_version():
    runner = FakeRunner([(0, ""), (0, ""), (0, ""), (0, "sandboxed-devenv 0.1.0\n")])
    version = make_remote(runner).install_wheel(pathlib.Path("/b/sandboxed_devenv-0.1.0-py3-none-any.whl"))
    assert version == "sandboxed-devenv 0.1.0"
    assert runner.commands[0][0] == "scp"
    assert runner.commands[0][-1] == "dev@192.168.64.19:/tmp/sandboxed_devenv-0.1.0-py3-none-any.whl"
    # No `~` anywhere: the far shell gets the command quoted and would not expand one.
    assert runner.commands[1][-1] == "sudo pipx install --global --force /tmp/sandboxed_devenv-0.1.0-py3-none-any.whl"
    assert runner.commands[2][-1] == "rm -f /tmp/sandboxed_devenv-0.1.0-py3-none-any.whl"
    assert runner.commands[3][-1] == f"{common.GLOBAL_COMMAND_PATH} --version"


def test_wait_for_ssh_and_wait_ready_poll(monkeypatch):
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    runner = FakeRunner([(255, ""), (255, ""), (0, "")])
    waited = make_remote(runner).wait_for_ssh(timeout=60, interval=0)
    assert waited >= 0
    assert len(runner.commands) == 3
    runner = FakeRunner([(0, json.dumps({"ready": False})), (1, ""), (0, json.dumps({"ready": True}))])
    report = make_remote(runner).wait_ready(timeout=60, interval=0)
    assert report["ready"] is True


def test_wait_ready_times_out(monkeypatch):
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: None)
    clock = iter([0, 0, 100, 100, 100, 100])
    monkeypatch.setattr(remote.time, "monotonic", lambda: next(clock))
    runner = FakeRunner([(0, json.dumps({"ready": False})), (0, json.dumps({"ready": False}))])
    with pytest.raises(remote.RemoteError):
        make_remote(runner).wait_ready(timeout=10, interval=0)


def test_wait_for_cloud_init_reports_the_last_line_or_fails():
    runner = FakeRunner([(0, "...\nstatus: done\n")])
    assert make_remote(runner).wait_for_cloud_init(timeout=5) == "status: done"
    assert runner.kwargs[0]["timeout"] == 5
    runner = FakeRunner([(1, "status: error\n"), (0, "status: error\ndetail: boom\n")])
    with pytest.raises(remote.RemoteError) as raised:
        make_remote(runner).wait_for_cloud_init(timeout=5)
    assert "boom" in str(raised.value)


def test_dry_run_prints_the_command_and_succeeds(capsys):
    target = make_remote(common.Runner(dry_run=True))
    completed = target.run(["true"])
    assert completed.returncode == 0
    assert capsys.readouterr().out.startswith("would run: ssh -F none")
    assert target.tool(["backend", "status"], parse_json=True) == {}


def test_parse_client_host():
    assert remote.parse_client_host("local") is None
    assert remote.parse_client_host("ssh://jsw@192.168.64.10") == ("jsw", "192.168.64.10", 22)
    assert remote.parse_client_host("ssh://jsw@desk.local:2222") == ("jsw", "desk.local", 2222)
    for bad in ("desk", "ssh://192.168.64.10", "http://jsw@x"):
        with pytest.raises(remote.RemoteError):
            remote.parse_client_host(bad)


def test_backend_and_client_host_use_the_control_key_and_per_machine_known_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    with pytest.raises(remote.RemoteError):
        remote.backend("192.168.64.21", "idesbx-x")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "control_ed25519").write_text("key")
    target = remote.backend("192.168.64.21", "idesbx-x")
    assert target.user == "dev"
    assert target.key == tmp_path / "secrets" / "control_ed25519"
    assert remote.sandbox_key_path() == tmp_path / "secrets" / "sandbox_ed25519"
    assert target.known_hosts == tmp_path / "known_hosts" / "idesbx-x"
    assert target.known_hosts.parent.is_dir()
    target.known_hosts.write_text("192.168.64.21 ssh-ed25519 AAAA\n")
    remote.forget_host_keys("idesbx-x")
    assert not target.known_hosts.exists()
    assert isinstance(remote.client_host(config.UserConfig()), remote.Local)
    desk = remote.client_host(config.UserConfig(client_host="ssh://jsw@192.168.64.10"))
    assert (desk.user, desk.host) == ("jsw", "192.168.64.10")
    assert desk.known_hosts == tmp_path / "known_hosts" / "client-host"


def test_local_runs_the_command_here(tmp_path):
    runner = FakeRunner([(0, json.dumps({"ok": 1}))])
    local = remote.Local(command_path="/usr/local/bin/sandboxed-devenv", runner=runner)
    assert local.tool(["connect", "ls"], parse_json=True) == {"ok": 1}
    assert runner.commands[0] == ["/usr/local/bin/sandboxed-devenv", "connect", "ls", "--json"]
    source = tmp_path / "a.whl"
    source.write_text("wheel")
    local.push([source], str(tmp_path / "b.whl"))
    assert (tmp_path / "b.whl").read_text() == "wheel"


def test_host_key_generation_and_pinning(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    private, public = remote.generate_host_key("idesbx-x")
    assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public.startswith("ssh-ed25519 ") and public.endswith(" idesbx-x")
    pinned = remote.pin_host_key("idesbx-x", "192.168.64.21", public)
    assert pinned == tmp_path / "known_hosts" / "idesbx-x"
    assert pinned.read_text() == f"192.168.64.21 {' '.join(public.split()[:2])}\n"
    remote.pin_host_key("idesbx-x", "192.168.64.21", "ssh-ed25519 OTHER")
    assert pinned.read_text() == "192.168.64.21 ssh-ed25519 OTHER\n"
