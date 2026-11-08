"""files/manifest.yaml, the files/<role>/ tree, the naming rule and the header lines agree.

PLAN.md, "Files and templates": the name is the destination (leading slash dropped, slashes as
`__`, dots kept), the extension is the format, a trailing `.tmpl` marks a template, a variable
path segment is `@@NAME@@`, and each file opens with a header naming its role and destination.
"""

import re

import pytest
import yaml

from sandboxed_devenv import render

MANIFEST = render.load_manifest()
FILES = render.files_root()
ENTRIES = []
for role_, entries_ in MANIFEST.items():
    for name_ in entries_:
        ENTRIES.append((role_, name_))
TEST_VALUES = render.sample_values(CONTROL_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test")
# What may follow the encoded destination in a name: an added format extension, then .tmpl.
NAME_TAIL = re.compile(r"(\.[a-z][a-z0-9_]*)?(\.tmpl)?")


def header_line(name: str, text: str) -> str | None:
    """The header with its comment syntax stripped; None for formats that have no comments."""
    if name.endswith((".json", ".json.tmpl")):
        return None
    lines = text.splitlines()
    index = 0
    if lines[0].startswith(("#cloud-config", "#!")):
        index = 1
    line = lines[index]
    for marker in ("<!--", "#"):
        if line.startswith(marker):
            line = line[len(marker):]
            break
    retval = line.strip()
    return retval


def test_every_role_directory_is_in_the_manifest_and_vice_versa():
    on_disk = set()
    for path in FILES.iterdir():
        if path.is_dir():
            on_disk.add(path.name)
    assert on_disk == set(MANIFEST)
    for role in MANIFEST:
        assert role in render.ROLES


@pytest.mark.parametrize("role", sorted(MANIFEST))
def test_every_file_has_an_entry_and_every_entry_a_file(role):
    on_disk = set()
    for path in (FILES / role).iterdir():
        assert path.is_file(), f"{path} is not a file"
        on_disk.add(path.name)
    assert on_disk == set(MANIFEST[role])


@pytest.mark.parametrize("role,name", ENTRIES)
def test_name_encodes_the_destination(role, name):
    entry = MANIFEST[role][name]
    if entry.is_cloudinit:
        assert name.startswith(render.CLOUDINIT_PREFIX)
        kind, file_name = render.cloudinit_file_name(name)
        assert file_name in ("user-data", "meta-data", "network-config"), name
        return
    assert entry.dest.startswith("/")
    encoded = render.encoded_dest(entry.dest)
    assert name.startswith(encoded), f"{name} does not start with {encoded}"
    assert NAME_TAIL.fullmatch(name[len(encoded):]), f"{name}: unexpected tail after {encoded}"


@pytest.mark.parametrize("role,name", ENTRIES)
def test_template_flag_matches_the_suffix(role, name):
    assert MANIFEST[role][name].template == name.endswith(render.TEMPLATE_SUFFIX)


@pytest.mark.parametrize("role,name", ENTRIES)
def test_ownership_and_mode_are_given_for_files_and_not_for_cloudinit(role, name):
    entry = MANIFEST[role][name]
    if entry.is_cloudinit:
        assert entry.owner is None and entry.group is None and entry.mode is None
        assert entry.stage is None or role == "backend"
    else:
        assert entry.owner and entry.group
        assert re.fullmatch(r"0[0-7]{3}", entry.mode), f"{name}: mode {entry.mode!r} must be four octal digits, quoted"
    if role == "backend" and not entry.is_cloudinit:
        assert entry.stage in ("image", "project")
    assert entry.description.strip()


@pytest.mark.parametrize("role,name", ENTRIES)
def test_header_names_the_role_and_the_destination(role, name):
    entry = MANIFEST[role][name]
    text = render.source_path(role, name).read_text()
    header = header_line(name, text)
    if header is None:
        return
    assert header.startswith(f"sandboxed-devenv {role}:"), f"{name}: header is {header!r}"
    if entry.is_cloudinit:
        assert "cloud-init" in header
    else:
        assert entry.dest in header, f"{name}: header does not name {entry.dest}"


@pytest.mark.parametrize("role,name", ENTRIES)
def test_rendering_leaves_no_placeholder(role, name):
    entry = MANIFEST[role][name]
    text = render.render_entry(role, name, entry, TEST_VALUES)
    assert "@@" not in text
    if not entry.template:
        assert "@@" not in render.source_path(role, name).read_text(), f"{name}: placeholders in a literal file"
    if not entry.is_cloudinit:
        assert "@@" not in str(render.rendered_dest(entry, TEST_VALUES))


@pytest.mark.parametrize("role,name", ENTRIES)
def test_cloudinit_documents_are_cloud_config_and_parse(role, name):
    entry = MANIFEST[role][name]
    if not entry.is_cloudinit:
        return
    text = render.render_entry(role, name, entry, TEST_VALUES)
    kind, file_name = render.cloudinit_file_name(name)
    if file_name == "user-data":
        assert text.startswith("#cloud-config\n")
    assert isinstance(yaml.safe_load(text), dict)


def test_fill_refuses_a_placeholder_without_a_value():
    with pytest.raises(render.RenderError, match="NOPE"):
        render.fill("x @@NOPE@@ y", {}, "where")


def test_render_role_writes_the_tree(tmp_path):
    written = render.render_role("host", TEST_VALUES, tmp_path)
    assert (tmp_path / "host" / "cloudinit" / "user-data") in written
    assert (tmp_path / "host" / "files" / "etc" / "nftables.conf") in written
    branch = TEST_VALUES["GATEWAY_BRANCH"]
    gateway_dir = tmp_path / "host" / "files" / "home" / "gw" / ".config" / "JetBrains" / f"JetBrainsGateway{branch}"
    assert (gateway_dir / "options" / "sshConfigs.xml").is_file()
    host_user_data = (tmp_path / "host" / "cloudinit" / "user-data").read_text()
    assert TEST_VALUES["CONTROL_PUBKEY"] in host_user_data
    assert TEST_VALUES["SANDBOX_PUBKEY"] not in host_user_data.replace(TEST_VALUES["CONTROL_PUBKEY"], "")
    assert TEST_VALUES["HOST_KEY_PUBLIC"] in host_user_data


def test_render_role_backend_writes_both_stages(tmp_path):
    written = render.render_role("backend", TEST_VALUES, tmp_path)
    assert (tmp_path / "backend" / "cloudinit-image" / "user-data") in written
    assert (tmp_path / "backend" / "cloudinit-project" / "user-data") in written
    assert (tmp_path / "backend" / "cloudinit" / "network-config") in written
    settings = (tmp_path / "backend" / "files" / "home" / "dev" / ".claude" / "settings.json").read_text()
    assert '"env": {}' in settings
    project_doc = yaml.safe_load((tmp_path / "backend" / "cloudinit-project" / "user-data").read_text())
    assert project_doc["hostname"] == TEST_VALUES["HOSTNAME"]
    assert project_doc["write_files"][0]["path"] == "/etc/sandboxed-devenv/project.yaml"


def test_project_values_carry_the_token_only_when_given():
    from sandboxed_devenv import config

    project = render.sample_project()
    assert render.project_values(project, "h")["CLAUDE_ENV_JSON"] == "{}"
    project.claude_oauth_token = "tok"
    values = render.project_values(project, "h", "192.168.64.21")
    assert "tok" in values["CLAUDE_ENV_JSON"]
    assert values["ADDRESS"] == "192.168.64.21"
    import base64

    assert config.ProjectConfig(**yaml.safe_load(base64.b64decode(values["PROJECT_YAML_B64"]))).claude_oauth_token == "tok"


def test_render_without_the_key_pairs_uses_placeholders(tmp_path, monkeypatch, capsys):
    """CI has no secrets: the documents render with placeholder keys and the run says so."""
    from sandboxed_devenv import cli

    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    monkeypatch.setattr(render, "wheel_build_command", lambda out_dir: ["true"])
    status = cli.main(["render", "--out", str(tmp_path / "out")])
    assert status == 0
    out = capsys.readouterr().out
    assert "placeholder key" in out
    user_data = (tmp_path / "out" / "host" / "cloudinit" / "user-data").read_text()
    assert render.PUBKEY_PLACEHOLDER in user_data
    project = yaml.safe_load((tmp_path / "out" / "backend" / "cloudinit-project" / "user-data").read_text())
    assert project["ssh_keys"]["ed25519_public"].startswith("ssh-ed25519 ")
