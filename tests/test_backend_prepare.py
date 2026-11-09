"""`backend repo`'s files in a temporary home, and the status report's shape off a backend."""

import dataclasses
import grp
import json
import os
import pwd

import pytest

from sandboxed_devenv import backend_prepare, cli

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


@pytest.fixture
def paths(tmp_path):
    retval = backend_prepare.Paths(home=tmp_path, user=ME, group=MY_GROUP)
    return retval


def test_names():
    assert backend_prepare.split_repository("jeffsw/sandboxed_devenv") == ("jeffsw", "sandboxed_devenv")
    assert backend_prepare.alias("jeffsw", "sandboxed_devenv") == "github.com-jeffsw-sandboxed_devenv"
    assert backend_prepare.clone_url("jeffsw", "blog_hugo") == "git@github.com-jeffsw-blog_hugo:jeffsw/blog_hugo.git"
    with pytest.raises(ValueError):
        backend_prepare.split_repository("not-a-repository")


def test_add_mints_binds_and_is_idempotent(paths):
    first = backend_prepare.add_repository(paths, "jeffsw/sandboxed_devenv", "ide_sandbox", clone=False, hostname="vm")
    assert first["created"] is True
    assert first["public_key"].startswith("ssh-ed25519 ")
    assert first["fingerprint"].startswith("SHA256:")
    assert first["checkout"] is None
    key = paths.key_file("jeffsw", "sandboxed_devenv")
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    stanza = paths.stanza_file("jeffsw", "sandboxed_devenv").read_text()
    assert "# repository: jeffsw/sandboxed_devenv" in stanza
    assert "Host github.com-jeffsw-sandboxed_devenv" in stanza
    assert f"IdentityFile {key}" in stanza
    assert "IdentitiesOnly yes" in stanza
    assert oct(paths.ssh_dir.stat().st_mode & 0o777) == "0o700"
    second = backend_prepare.add_repository(paths, "jeffsw/sandboxed_devenv", "ide_sandbox", clone=False, hostname="vm")
    assert second["created"] is False
    assert second["public_key"] == first["public_key"]


def test_list_rekey_and_remove(paths):
    backend_prepare.add_repository(paths, "jeffsw/a", "p", clone=False, hostname="vm")
    backend_prepare.add_repository(paths, "other-org/b.js", "p", clone=False, hostname="vm")
    listed = backend_prepare.list_repositories(paths, "p")
    assert [record["repository"] for record in listed] == ["jeffsw/a", "other-org/b.js"]
    before = listed[0]["fingerprint"]
    rekeyed = backend_prepare.rekey_repository(paths, "jeffsw/a", "p", hostname="vm")
    assert rekeyed["created"] is True
    assert rekeyed["fingerprint"] != before
    removed = backend_prepare.remove_repository(paths, "jeffsw/a", "p", delete_checkout=False)
    assert len(removed["removed"]) == 3
    assert [record["repository"] for record in backend_prepare.list_repositories(paths, "p")] == ["other-org/b.js"]
    assert backend_prepare.remove_repository(paths, "jeffsw/a", "p", delete_checkout=False)["removed"] == []


def test_clone_needs_a_project(paths):
    with pytest.raises(RuntimeError, match="no-clone"):
        backend_prepare.add_repository(paths, "jeffsw/a", None, clone=True, hostname="vm")


def test_status_json_off_a_backend(capsys):
    status = cli.main(["backend", "status", "--json"])
    assert status == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["project"] is None
    assert "package_version" in report
    assert isinstance(report["ssh_host_keys"], list)


def test_prepare_refuses_off_linux_or_without_root(capsys):
    status = cli.main(["backend", "prepare", "--stage", "image"])
    assert status == 1
    err = capsys.readouterr().err
    assert "Linux" in err or "needs root" in err


@pytest.fixture
def backend_markers(tmp_path, monkeypatch):
    """The three /etc/sandboxed-devenv files moved into a temporary directory."""
    from sandboxed_devenv import config

    monkeypatch.setattr(config, "BACKEND_PROJECT_CONFIG", tmp_path / "project.yaml")
    monkeypatch.setattr(config, "BACKEND_PREPARED_MARKER", tmp_path / "prepared.yaml")
    monkeypatch.setattr(config, "BACKEND_IMAGE_MARKER", tmp_path / "image.yaml")
    retval = tmp_path
    return retval


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_status_and_project_name_without_reading_the_token_file(backend_markers, capsys):
    (backend_markers / "project.yaml").write_text("project: p\nclaude_oauth_token: secret\n")
    (backend_markers / "project.yaml").chmod(0)
    assert backend_prepare.project_name() is None
    status = cli.main(["backend", "status"])
    assert status == 0
    out = capsys.readouterr().out
    assert "unknown" in out
    assert "secret" not in out
    (backend_markers / "prepared.yaml").write_text("project: p\nrepositories: [jeffsw/a]\npackage_version: x\n")
    assert backend_prepare.project_name() == "p"
    status = cli.main(["backend", "status", "--json"])
    assert status == 0
    report = json.loads(capsys.readouterr().out)
    assert report["project"]["name"] == "p"
    assert report["project"]["repositories"] == ["jeffsw/a"]
    assert "secret" not in json.dumps(report)


def test_ide_registration_is_a_link_to_the_ide_directory(tmp_path):
    ide_dir = tmp_path / "opt" / "jetbrains" / "idea"
    ide_dir.mkdir(parents=True)
    registration = tmp_path / "userProvidedDist"
    assert backend_prepare.ide_registered(registration, ide_dir) is False
    registration.mkdir()
    (registration / "other").symlink_to(tmp_path / "opt")
    assert backend_prepare.ide_registered(registration, ide_dir) is False
    (registration / "idea").symlink_to(ide_dir)
    assert backend_prepare.ide_registered(registration, ide_dir) is True


def test_project_dir_is_made_at_first_boot(paths):
    from sandboxed_devenv import config, steps

    project = config.ProjectConfig(project="p", git=config.GitIdentity(user_name="D", user_email="d@example.invalid"))
    ctx = backend_prepare.Context(apply=True, values={}, command="backend prepare", stage="project", project=project, paths=paths)
    first = backend_prepare.step_project_dir(ctx)
    assert [result.status for result in first] == [steps.CHANGED]
    assert (paths.proj_dir / "p").is_dir()
    assert oct((paths.proj_dir / "p").stat().st_mode & 0o777) == "0o755"
    assert [result.status for result in backend_prepare.step_project_dir(ctx)] == [steps.UNCHANGED]
    assert backend_prepare.step_project_dir in backend_prepare.PROJECT_STEPS


def test_a_copy_moves_the_source_projects_directory_and_keeps_its_checkouts(paths):
    from sandboxed_devenv import config, steps

    identity = config.GitIdentity(user_name="D", user_email="d@example.invalid")
    copy = config.ProjectConfig(project="q", git=identity, cloned_from="p")
    ctx = backend_prepare.Context(apply=True, values={}, command="backend prepare", stage="project", project=copy, paths=paths)
    (paths.proj_dir / "p" / "a").mkdir(parents=True)
    (paths.proj_dir / "p" / "a" / "work.txt").write_text("uncommitted")
    moved = backend_prepare.step_inherited_project_dir(ctx)
    assert [result.status for result in moved] == [steps.CHANGED]
    assert "moved from" in moved[0].detail
    assert (paths.proj_dir / "q" / "a" / "work.txt").read_text() == "uncommitted"
    assert not (paths.proj_dir / "p").exists()
    again = backend_prepare.step_inherited_project_dir(ctx)
    assert [(result.status, result.detail) for result in again] == [(steps.UNCHANGED, "present")]
    assert [result.status for result in backend_prepare.step_project_dir(ctx)] == [steps.UNCHANGED]
    kept = backend_prepare.add_repository(paths, "jeffsw/a", "q", clone=True, hostname="vm")
    assert kept["cloned"] is False
    assert kept["checkout"] == str(paths.proj_dir / "q" / "a")
    # Not a copy: nothing to do. Both directories present: left alone.
    plain = config.ProjectConfig(project="q", git=identity)
    assert backend_prepare.step_inherited_project_dir(dataclasses.replace(ctx, project=plain)) == []
    (paths.proj_dir / "p").mkdir()
    both = backend_prepare.step_inherited_project_dir(ctx)
    assert both[0].status == steps.UNCHANGED and "left alone" in both[0].detail
    assert (paths.proj_dir / "p").is_dir() and (paths.proj_dir / "q").is_dir()
    plan = backend_prepare.PROJECT_STEPS
    assert plan.index(backend_prepare.step_inherited_project_dir) < plan.index(backend_prepare.step_project_dir)
