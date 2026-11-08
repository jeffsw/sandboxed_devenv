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
