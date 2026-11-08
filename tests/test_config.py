"""config.yaml, project.yaml and versions.yaml models."""

import pathlib

import pytest
import yaml

from sandboxed_devenv import config


def test_defaults_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    user_config = config.load_user_config()
    assert user_config.client_host == "local"
    assert user_config.vm_name("ide_sandbox") == "idesbx-ide_sandbox"
    assert (user_config.backend.cpus, user_config.backend.memory_gib, user_config.backend.disk_gib) == (4, 8, 32)
    assert user_config.utm.address_pool_start == "192.168.64.20"
    assert config.secrets_dir() == tmp_path / "secrets"
    assert config.images_dir() == tmp_path / "images"


def test_config_file_is_read_and_unknown_keys_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("client_host: ssh://jsw@192.168.64.10\nvm_name_prefix: sbx-\nbackend:\n  cpus: 6\n")
    user_config = config.load_user_config()
    assert user_config.client_host == "ssh://jsw@192.168.64.10"
    assert user_config.vm_name("x") == "sbx-x"
    assert user_config.backend.cpus == 6
    assert user_config.backend.memory_gib == 8
    (tmp_path / "config.yaml").write_text("clienthost: local\n")
    with pytest.raises(Exception):
        config.load_user_config()


def test_project_config_round_trips(tmp_path):
    project = config.ProjectConfig(
        project="ide_sandbox",
        repositories=[config.Repository(name="jeffsw/sandboxed_devenv", access="write")],
        git=config.GitIdentity(user_name="Jeff Wheeler", user_email="jeffsw6@gmail.com"),
        claude_oauth_token="secret-value",
    )
    text = project.to_yaml()
    assert "secret-value" in text
    path = tmp_path / "project.yaml"
    path.write_text(text)
    loaded = config.load_project_config(path)
    assert loaded == project
    assert loaded.repositories[0].owner == "jeffsw"
    assert loaded.repositories[0].repo == "sandboxed_devenv"
    assert config.load_project_config(tmp_path / "absent.yaml") is None
    with pytest.raises(Exception):
        config.Repository(name="no-slash")
    with pytest.raises(Exception):
        config.ProjectConfig(project="has space", git=project.git)


def test_versions_load_and_urls():
    versions = config.load_versions()
    assert versions.gateway.branch == ".".join(versions.gateway.version.split(".")[:2])
    assert versions.gateway.version in versions.gateway.url("aarch64")
    assert versions.gateway.url("aarch64").endswith("-aarch64.tar.gz")
    assert versions.ide_backend.version in versions.ide_backend.url("x86_64")
    assert versions.ubuntu.image_serial in versions.ubuntu.url("aarch64")
    assert "com.anthropic.code.plugin" in versions.ide_backend.plugins
    with pytest.raises(ValueError):
        versions.gateway.url("riscv64")


def test_versions_file_is_the_only_place_with_version_strings():
    """No other source file carries the Gateway or IDE version."""
    versions = config.load_versions()
    package = pathlib.Path(config.__file__).parent
    for path in package.glob("*.py"):
        text = path.read_text()
        assert versions.gateway.version not in text, f"{path.name} carries the Gateway version"
        assert versions.ide_backend.version not in text, f"{path.name} carries the IDE version"
    raw = yaml.safe_load(config.versions_file().read_text())
    assert set(raw) == {"gateway", "ide_backend", "client", "ubuntu"}


def test_secrets_may_be_values_or_1password_references(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    assert config.secret_source("claude_oauth_token") == ("absent", None)
    assert config.read_secret("claude_oauth_token") is None
    (secrets / "claude_oauth_token").write_text("plain-value\n")
    assert config.secret_source("claude_oauth_token") == ("file", None)
    assert config.read_secret("claude_oauth_token") == "plain-value"
    (secrets / "claude_oauth_token").write_text("op://Private/Claude Code/credential\n")
    assert config.secret_source("claude_oauth_token") == ("1password", "op://Private/Claude Code/credential")
    asked = []

    def resolver(reference):
        asked.append(reference)
        retval = "resolved-value"
        return retval

    assert config.read_secret("claude_oauth_token", resolver) == "resolved-value"
    assert asked == ["op://Private/Claude Code/credential"]
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    with pytest.raises(config.SecretError, match="1Password CLI"):
        config.read_secret("claude_oauth_token")
    monkeypatch.setattr(config.shutil, "which", lambda name: "/opt/homebrew/bin/op")

    def failing(command, **kwargs):
        import subprocess
        retval = subprocess.CompletedProcess(command, 1, "", "[ERROR] not signed in")
        return retval

    monkeypatch.setattr(config.subprocess, "run", failing)
    with pytest.raises(config.SecretError, match="not signed in"):
        config.read_secret("claude_oauth_token")


def test_backend_defaults_carry_the_token_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    assert config.UserConfig().backend.claude_token == "shared"
    assert config.UserConfig().backend.disposable_claude_token is False
    (tmp_path / "config.yaml").write_text("backend:\n  claude_token: per-project\n  disposable_claude_token: true\n")
    user_config = config.load_user_config()
    assert user_config.backend.claude_token == "per-project"
    assert user_config.backend.disposable_claude_token is True
    (tmp_path / "config.yaml").write_text("backend:\n  claude_token: sometimes\n")
    with pytest.raises(Exception):
        config.load_user_config()
