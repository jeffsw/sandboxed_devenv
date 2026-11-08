"""`repo add / rm / ls`, `creds ls / rotate / audit`, and the GitHub client, against the fakes in conftest."""

import json
import socket
import subprocess

import pytest

from sandboxed_devenv import cli, common, control_creds, control_inventory


class FakeRunner:
    def __init__(self, outputs=None):
        self.commands = []
        self.inputs = []
        self.outputs = list(outputs or [])
        self.dry_run = False
        self.verbose = False

    def run(self, command, **kwargs):
        self.commands.append(command)
        self.inputs.append(kwargs.get("input"))
        if self.outputs:
            status, stdout, stderr = self.outputs.pop(0)
        else:
            status, stdout, stderr = 0, "", ""
        retval = subprocess.CompletedProcess(command, status, stdout, stderr)
        return retval


def test_key_text_title_and_token_fingerprint():
    assert control_creds.key_text("ssh-ed25519 AAAA comment here") == "ssh-ed25519 AAAA"
    assert control_creds.key_text("ssh-ed25519 AAAA") == "ssh-ed25519 AAAA"
    assert control_creds.key_title("ide_sandbox") == "sandboxed-devenv ide_sandbox"
    assert control_creds.token_fingerprint("tok").startswith("sha256:")
    assert len(control_creds.token_fingerprint("tok")) == len("sha256:") + 16
    assert control_creds.find_key([{"id": 1, "key": "ssh-ed25519 AAAA"}], "ssh-ed25519 AAAA c")["id"] == 1
    assert control_creds.find_key([{"id": 1, "key": "ssh-ed25519 AAAA"}], None) is None


def test_github_client_runs_gh_api():
    runner = FakeRunner([
        (0, json.dumps([{"id": 5, "key": "ssh-ed25519 X", "title": "t", "read_only": True}]), ""),
        (0, json.dumps({"id": 6, "key": "ssh-ed25519 Y", "title": "sandboxed-devenv p", "read_only": False}), ""),
        (0, "", ""),
        (1, "", "gh: Not Found (HTTP 404)"),
        (1, "", "gh: Bad credentials (HTTP 401)"),
    ])
    github = control_creds.GitHub(runner)
    assert github.list_deploy_keys("o/r")[0]["id"] == 5
    assert runner.commands[0] == ["gh", "api", "--method", "GET", "repos/o/r/keys?per_page=100"]
    created = github.add_deploy_key("o/r", "ssh-ed25519 Y comment", "sandboxed-devenv p", read_only=False)
    assert created["id"] == 6
    assert runner.commands[1] == ["gh", "api", "--method", "POST", "repos/o/r/keys", "--input", "-"]
    assert json.loads(runner.inputs[1]) == {"title": "sandboxed-devenv p", "key": "ssh-ed25519 Y", "read_only": False}
    assert github.delete_deploy_key("o/r", 6) is True
    assert runner.commands[2] == ["gh", "api", "--method", "DELETE", "repos/o/r/keys/6"]
    assert github.delete_deploy_key("o/r", 7) is False
    with pytest.raises(control_creds.GitHubError):
        github.list_deploy_keys("o/r")
    dry = control_creds.GitHub(common.Runner(dry_run=True))
    assert dry.add_deploy_key("o/r", "ssh-ed25519 Z", "t", True)["id"] == 0
    assert dry.list_deploy_keys("o/r") == []


def test_repo_add_registers_clones_and_is_idempotent(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "repo", "add", "p", "jeffsw/x"]) == 0
    assert "would run `backend repo add jeffsw/x --no-clone`" in capsys.readouterr().out
    assert cli.main(["repo", "add", "p", "jeffsw/x", "--deploy-key-grants-repo-write"]) == 0
    out = capsys.readouterr().out
    assert "key pair minted in idesbx-p" in out
    assert "registered on GitHub as id 1001 (write)" in out
    assert "cloned to /home/dev/proj/" in out
    backend = rig["backends"]["192.168.64.20"]
    assert ("tool", ("backend", "repo", "add", "jeffsw/x", "--no-clone")) in backend.calls
    assert ("tool", ("backend", "repo", "add", "jeffsw/x")) in backend.calls
    assert ("add", "jeffsw/x", "sandboxed-devenv p", False) in rig["github"].calls
    record = control_inventory.require("p")
    assert [(r.name, r.access, r.state, r.github_key_id) for r in record.repos] == [("jeffsw/x", "write", "active", 1001)]
    assert cli.main(["repo", "add", "p", "jeffsw/x"]) == 0
    assert "already registered (id 1001)" in capsys.readouterr().out
    assert len(rig["github"].keys["jeffsw/x"]) == 1
    assert cli.main(["repo", "add", "p", "jeffsw/y", "--no-clone"]) == 0
    assert control_inventory.require("p").repos[1].checkout is None
    assert ("tool", ("backend", "repo", "add", "jeffsw/y")) not in backend.calls
    assert cli.main(["repo", "ls", "p"]) == 0
    out = capsys.readouterr().out
    assert "jeffsw/x" in out and "active" in out and "1001" in out and "not cloned" in out
    assert cli.main(["repo", "ls", "p", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "jeffsw/x"
    assert cli.main(["repo", "add", "p", "not-a-repo"]) == 1


def test_repo_add_adopts_a_key_github_already_holds(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    backend = rig["backends"]["192.168.64.20"]
    minted = backend.tool(["backend", "repo", "add", "jeffsw/x", "--no-clone"], parse_json=True, json_flag=False)
    planted = rig["github"].plant("jeffsw/x", minted["public_key"], "by hand")
    capsys.readouterr()
    assert cli.main(["repo", "add", "p", "jeffsw/x"]) == 0
    out = capsys.readouterr().out
    assert f"already on GitHub as id {planted['id']} (by hand); adopted" in out
    assert control_inventory.require("p").repos[0].github_key_id == planted["id"]
    assert not any(call[0] == "add" for call in rig["github"].calls)


def test_repo_rm_revokes_then_removes_and_keeps_pending_on_failure(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a", "--repo", "jeffsw/b"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "repo", "rm", "p", "jeffsw/a"]) == 0
    assert "would revoke deploy key 1001" in capsys.readouterr().out
    assert cli.main(["repo", "rm", "p", "jeffsw/a", "--delete-checkout"]) == 0
    out = capsys.readouterr().out
    assert "deploy key id 1001 revoked" in out
    assert "3 files removed in idesbx-p" in out
    backend = rig["backends"]["192.168.64.20"]
    assert ("tool", ("backend", "repo", "rm", "jeffsw/a", "--delete-checkout")) in backend.calls
    assert [r.name for r in control_inventory.require("p").repos] == ["jeffsw/b"]
    rig["github"].fail = True
    assert cli.main(["repo", "rm", "p", "jeffsw/b"]) == 1
    assert "pending-revoke" in capsys.readouterr().err
    record = control_inventory.require("p")
    assert record.repos[0].state == "pending-revoke"
    assert ("tool", ("backend", "repo", "rm", "jeffsw/b")) not in backend.calls
    assert cli.main(["repo", "rm", "p", "jeffsw/nope"]) == 1


def test_creds_ls_lists_keys_the_token_and_the_key_pairs(rig, capsys):
    (rig["home"] / "secrets" / "claude_oauth_token").write_text("secret-value-xyz\n")
    assert cli.main(["creds", "ls"]) == 0
    out = capsys.readouterr().out
    assert "control-key" in out and "sandbox-key" in out
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    capsys.readouterr()
    assert cli.main(["creds", "ls", "p", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    kinds = [row["kind"] for row in rows]
    assert kinds == ["control-key", "sandbox-key", "deploy-key", "claude-token"]
    assert rows[2]["id"] == 1001 and rows[2]["state"] == "active"
    assert rows[3]["fingerprint"].startswith("sha256:")
    assert "secret-value-xyz" not in json.dumps(rows)  # only the hash, never the token
    assert cli.main(["creds", "ls"]) == 0
    out = capsys.readouterr().out
    assert "deploy-key" in out and "claude-token" in out and "revocation:" in out
    assert cli.main(["creds", "ls", "nope"]) == 1


def test_creds_rotate_deploy_keys_and_token(rig, capsys):
    (rig["home"] / "secrets" / "claude_oauth_token").write_text("tok-1\n")
    assert cli.main(["new", "p", "--repo", "jeffsw/a", "--repo", "jeffsw/b"]) == 0
    capsys.readouterr()
    assert cli.main(["creds", "rotate", "p"]) == 2
    assert cli.main(["--dry-run", "creds", "rotate", "p", "--deploy-key", "--claude-token"]) == 0
    out = capsys.readouterr().out
    assert "would rekey every repository" in out and "would write the token" in out
    assert cli.main(["creds", "rotate", "p", "--deploy-key", "jeffsw/a"]) == 0
    out = capsys.readouterr().out
    assert "new key pair in idesbx-p (SHA256:jeffsw/a-g1)" in out
    assert "deploy key id 1001 revoked" in out
    assert "registered on GitHub as id 1003" in out
    record = control_inventory.require("p")
    assert record.repos[0].github_key_id == 1003 and record.repos[0].state == "active"
    assert record.repos[1].github_key_id == 1002
    assert [item["id"] for item in rig["github"].keys["jeffsw/a"]] == [1003]
    (rig["home"] / "secrets" / "claude_oauth_token").write_text("tok-2\n")
    assert cli.main(["creds", "rotate", "p", "--claude-token"]) == 0
    assert "Claude token in idesbx-p replaced" in capsys.readouterr().out
    backend = rig["backends"]["192.168.64.20"]
    assert "claude_oauth_token: tok-2" in backend.inputs[-1]
    assert ("tool", ("backend", "prepare", "--stage", "project")) in backend.calls
    record = control_inventory.require("p")
    assert record.credentials[0].fingerprint == control_creds.token_fingerprint("tok-2")
    assert cli.main(["creds", "rotate", "p", "--deploy-key", "jeffsw/nope"]) == 1
    (rig["home"] / "secrets" / "claude_oauth_token").unlink()
    assert cli.main(["creds", "rotate", "p", "--claude-token"]) == 1


def test_creds_audit_adopts_retries_reports_and_fixes(rig, capsys):
    assert cli.main(["creds", "audit"]) == 0
    assert "nothing to compare" in capsys.readouterr().out
    github = rig["github"]
    github.fail = True
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 1
    github.fail = False
    record = control_inventory.require("p")
    assert record.repos[0].state == "unregistered"
    planted = github.plant("jeffsw/a", record.repos[0].public_key, "idesbx-p by hand")
    stale = github.plant("jeffsw/a", "ssh-ed25519 OLDKEY", "sandboxed-devenv old-project")
    foreign = github.plant("jeffsw/a", "ssh-ed25519 CIKEY", "ci deploy key")
    capsys.readouterr()
    assert cli.main(["creds", "audit"]) == 1
    out = capsys.readouterr().out
    assert f"adopted key id {planted['id']} (idesbx-p by hand)" in out
    assert f"unclaimed key id {stale['id']} (sandboxed-devenv old-project" in out and "--fix deletes it" in out
    assert f"unclaimed key id {foreign['id']} (ci deploy key" in out and "left alone" in out
    assert "2 problem(s)" in out
    record = control_inventory.require("p")
    assert record.repos[0].state == "active" and record.repos[0].github_key_id == planted["id"]
    assert cli.main(["creds", "audit", "--fix"]) == 1
    out = capsys.readouterr().out
    assert f"deleted unclaimed key id {stale['id']}" in out
    assert "1 problem(s)" in out
    assert [item["id"] for item in github.keys["jeffsw/a"]] == [planted["id"], foreign["id"]]
    github.keys["jeffsw/a"] = [item for item in github.keys["jeffsw/a"] if item["id"] != planted["id"]]
    assert cli.main(["creds", "audit"]) == 1
    assert "is gone from GitHub; marked unregistered" in capsys.readouterr().out
    assert control_inventory.require("p").repos[0].state == "unregistered"


def test_creds_audit_finishes_a_removed_backend(rig, capsys):
    assert cli.main(["new", "p", "--repo", "jeffsw/a"]) == 0
    rig["github"].fail = True
    assert cli.main(["rm", "p", "--yes"]) == 1
    rig["github"].fail = False
    capsys.readouterr()
    assert cli.main(["creds", "audit"]) == 0
    out = capsys.readouterr().out
    assert "deploy key id 1001 revoked" in out
    assert "the record of the removed backend is gone" in out
    assert control_inventory.load("p") is None
    assert rig["github"].keys["jeffsw/a"] == []
    assert cli.main(["creds", "ls"]) == 0


def test_ensure_keys_renames_the_legacy_pair_and_mints_the_sandbox_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "gw_ed25519").write_text("old private")
    (secrets / "gw_ed25519.pub").write_text("ssh-ed25519 OLD gw@mac\n")
    control, sandbox = control_creds.ensure_keys()
    out = capsys.readouterr().out
    assert "renamed to control_ed25519" in out and "sandbox key pair created" in out
    assert control == secrets / "control_ed25519" and control.read_text() == "old private"
    assert not (secrets / "gw_ed25519").exists()
    assert control_creds.control_public_key() == "ssh-ed25519 OLD gw@mac"
    sandbox_public = control_creds.sandbox_public_key()
    assert sandbox_public.startswith("ssh-ed25519 ")
    assert sandbox_public.endswith(f"gw@{socket.gethostname()}")
    assert oct(sandbox.stat().st_mode & 0o777) == "0o600"
    again = control_creds.ensure_keys()
    assert again == (control, sandbox)
    assert capsys.readouterr().out == ""
    keys = control_creds.seed_key_values(False, None)
    assert keys["CONTROL_PUBKEY"] == "ssh-ed25519 OLD gw@mac"
    assert keys["SANDBOX_PUBKEY"] == control_creds.sandbox_public_key()
    sandbox.unlink()
    sandbox.with_suffix(".pub").unlink()
    assert control_creds.seed_key_values(True, None)["SANDBOX_PUBKEY"] == "ssh-ed25519 (minted on the real run)"
    assert not sandbox.exists()
