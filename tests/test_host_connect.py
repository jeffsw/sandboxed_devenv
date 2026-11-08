"""`connect add / rm / ls`: the stanza, the pinned keys, Gateway's XML, in a temporary home."""

import grp
import json
import os
import pwd

import pytest

from sandboxed_devenv import cli, host_connect

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
KEYS = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA root@vm",
        "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBAAA"]
GATEWAY_XML = """<application>
  <component name="SshConfigs">
    <configs>
      <sshConfig host="192.168.64.20" id="8c05b60b-188a-439b-861b-899cbb9fda27" keyPath="$USER_HOME$/.ssh/vm_ed25519"
                 port="22" nameFormat="DESCRIPTIVE" username="dev" useOpenSSHConfig="true" />
    </configs>
  </component>
</application>
"""


@pytest.fixture
def paths(tmp_path, monkeypatch):
    retval = host_connect.Paths(home=tmp_path, user=ME, group=MY_GROUP, gateway_branch="2026.2")
    monkeypatch.setattr(host_connect, "default_paths", lambda: retval)
    monkeypatch.setattr(host_connect, "can_run", lambda command, dry_run: True)
    monkeypatch.setattr(host_connect, "inspectable", lambda dry_run: True)
    monkeypatch.setattr(host_connect, "gateway_running", lambda paths: False)
    return retval


def test_stanza_round_trip(paths):
    text = host_connect.stanza_text("ide_sandbox", "192.168.64.21", paths)
    assert "Host ide_sandbox 192.168.64.21" in text
    assert f"IdentityFile {paths.key_file}" in text
    assert "StrictHostKeyChecking yes" in text
    assert host_connect.parse_stanza(text) == ("ide_sandbox", "192.168.64.21")
    assert host_connect.parse_stanza("Host x\n") is None


def test_known_hosts_pinning():
    existing = "|1|hash|hash ssh-ed25519 AAAA\n192.168.64.20 ssh-ed25519 OLD\n[192.168.64.20]:2222 ssh-rsa PORT\n"
    pinned = host_connect.with_pinned_keys(existing, "192.168.64.20", host_connect.normalize_keys(KEYS))
    assert pinned == ("|1|hash|hash ssh-ed25519 AAAA\n"
                      f"192.168.64.20 {KEYS[0].rsplit(' ', 1)[0]}\n"
                      f"192.168.64.20 {KEYS[1]}\n")
    assert host_connect.without_pinned_keys(pinned, "192.168.64.20") == "|1|hash|hash ssh-ed25519 AAAA\n"
    assert host_connect.without_pinned_keys("", "x") == ""
    assert host_connect.normalize_keys(["a b c\nd e"]) == ["a b", "d e"]
    assert host_connect.normalize_keys(["lonely"]) == []


def test_gateway_xml_add_remove_list():
    with_entry = host_connect.ssh_configs_with(None, "p", "192.168.64.21", "2026.2")
    assert with_entry.startswith("<!-- sandboxed-devenv host: ")
    entries = host_connect.ssh_configs_entries(with_entry)
    assert entries == [{"host": "192.168.64.21", "id": host_connect.entry_id("p"), "username": "dev"}]
    assert 'keyPath="$USER_HOME$/.ssh/vm_ed25519"' in with_entry
    assert 'useOpenSSHConfig="true"' in with_entry
    again = host_connect.ssh_configs_with(with_entry, "p", "192.168.64.21", "2026.2")
    assert len(host_connect.ssh_configs_entries(again)) == 1
    merged = host_connect.ssh_configs_with(GATEWAY_XML, "q", "192.168.64.22", "2026.2")
    assert [entry["host"] for entry in host_connect.ssh_configs_entries(merged)] == ["192.168.64.20", "192.168.64.22"]
    replaced = host_connect.ssh_configs_with(GATEWAY_XML, "old", "192.168.64.20", "2026.2")
    assert [entry["id"] for entry in host_connect.ssh_configs_entries(replaced)] == [host_connect.entry_id("old")]
    without = host_connect.ssh_configs_without(merged, "q", "2026.2")
    assert [entry["host"] for entry in host_connect.ssh_configs_entries(without)] == ["192.168.64.20"]
    assert host_connect.ssh_configs_entries("") == []
    assert host_connect.entry_id("p") == host_connect.entry_id("p") != host_connect.entry_id("q")


def test_add_ls_rm_write_the_three_files(paths, capsys):
    status = cli.main(["connect", "add", "ide_sandbox", "192.168.64.21", *KEYS])
    assert status == 0
    out = capsys.readouterr().out
    assert "connect add: apply: 3 changed" in out
    stanza = paths.stanza_file("ide_sandbox")
    assert oct(stanza.stat().st_mode & 0o777) == "0o600"
    assert host_connect.parse_stanza(stanza.read_text()) == ("ide_sandbox", "192.168.64.21")
    known = paths.known_hosts.read_text()
    assert known.count("192.168.64.21 ") == 2
    assert "root@vm" not in known
    assert paths.ssh_configs.is_file()
    assert host_connect.ssh_configs_entries(paths.ssh_configs.read_text())[0]["host"] == "192.168.64.21"

    assert cli.main(["connect", "add", "ide_sandbox", "192.168.64.21", *KEYS]) == 0
    assert "3 unchanged" in capsys.readouterr().out

    assert cli.main(["connect", "ls", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == [{"project": "ide_sandbox", "address": "192.168.64.21", "pinned": True, "gateway_entry": True}]
    assert cli.main(["connect", "ls"]) == 0
    assert "ide_sandbox: 192.168.64.21 (pinned, Gateway entry)" in capsys.readouterr().out

    assert cli.main(["--dry-run", "connect", "rm", "ide_sandbox"]) == 0
    assert "would change" in capsys.readouterr().out
    assert stanza.exists()
    assert cli.main(["connect", "rm", "ide_sandbox"]) == 0
    assert not stanza.exists()
    assert "192.168.64.21" not in paths.known_hosts.read_text()
    assert host_connect.ssh_configs_entries(paths.ssh_configs.read_text()) == []
    assert cli.main(["connect", "ls"]) == 0
    assert "no connections" in capsys.readouterr().out


def test_add_and_rm_write_the_gateway_file_while_gateway_runs_and_say_so(paths, monkeypatch, capsys):
    monkeypatch.setattr(host_connect, "gateway_running", lambda paths: True)
    assert cli.main(["connect", "add", "p", "192.168.64.23", KEYS[0]]) == 0
    out = capsys.readouterr().out
    assert "Gateway is running" in out and "click another window, then Gateway's" in out and "No restart needed" in out
    assert host_connect.ssh_configs_entries(paths.ssh_configs.read_text())[0]["host"] == "192.168.64.23"
    assert cli.main(["connect", "rm", "p"]) == 0
    out = capsys.readouterr().out
    assert "gone from its dialog" in out
    assert host_connect.ssh_configs_entries(paths.ssh_configs.read_text()) == []


def test_add_needs_a_key(paths, capsys):
    assert cli.main(["connect", "add", "p", "192.168.64.23", "nonsense"]) == 2
    assert "host key" in capsys.readouterr().err


def test_dry_run_off_a_client_host_prints_the_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host_connect, "default_paths", lambda: host_connect.Paths(home=tmp_path, gateway_branch="2026.2"))
    monkeypatch.setattr(host_connect, "inspectable", lambda dry_run: False)
    assert cli.main(["--dry-run", "connect", "add", "p", "192.168.64.23", KEYS[0]]) == 0
    out = capsys.readouterr().out
    assert "the plan for p at 192.168.64.23" in out
    assert "1 key(s) pinned" in out
    assert cli.main(["--dry-run", "connect", "rm", "p"]) == 0
    assert "would remove" in capsys.readouterr().out


def test_arguments_for_the_control_side():
    assert host_connect.add_arguments("p", "192.168.64.23", ["ssh-ed25519 A"]) == ["connect", "add", "p", "192.168.64.23", "ssh-ed25519 A"]
    assert host_connect.rm_arguments("p") == ["connect", "rm", "p"]
