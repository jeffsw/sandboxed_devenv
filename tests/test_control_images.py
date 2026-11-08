"""`image build / ls / rm`: names, records, the rebuild decision, the seed, the plan, no VM touched."""

import datetime
import pathlib
import shutil
import subprocess

import pytest
import yaml

from sandboxed_devenv import cli, common, config, control_driver, control_images, control_inventory, remote, render

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test"
CHECKSUMS = """63a93bd5a8d76e33b15ceb5daa3657bd79be804748051ab178e643b0f5da22e7 *ubuntu-26.04-server-cloudimg-arm64.img
8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2 *ubuntu-26.04-server-cloudimg-amd64.img
"""


class FakeDriver:
    name = "fake"
    runner = common.Runner(dry_run=True)
    static_network = True
    raw_disk = True
    live_resources = False

    def available(self):
        retval = None
        return retval

    def address_pool(self, user_config):
        retval = control_inventory.utm_pool(user_config)
        return retval

    def image_exists(self, name):
        retval = control_images.disk_path(name).is_file()
        return retval

    def image_location(self, name):
        retval = str(control_images.disk_path(name))
        return retval

    def image_size(self, name):
        retval = (0, 0)
        return retval


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    retval = tmp_path
    return retval


def wanted() -> control_images.ImageRecord:
    user_config = config.UserConfig()
    values = control_images.seed_values(user_config, PUBKEY)
    user_data = render.cloudinit_document("backend", "cloudinit-image", "user-data", values)
    assert render.HOST_KEY_DIGEST_VALUES["HOST_KEY_PUBLIC"] in user_data
    retval = control_images.wanted_record(config.load_versions(), user_config, "aarch64", user_data, "utm")
    return retval


def test_names():
    assert control_images.image_name("0.1.0") == "backend-0.1.0"
    assert control_images.normalize_name("backend-0.1.0.img") == "backend-0.1.0"
    assert control_images.normalize_name("backend-0.1.0") == "backend-0.1.0"
    assert control_images.format_size(3 << 30) == "3.0 GiB"
    assert control_images.format_size(900 << 20) == "900 MiB"


def test_checksums():
    url = "https://cloud-images.ubuntu.com/releases/26.04/release-20260927/ubuntu-26.04-server-cloudimg-arm64.img"
    assert control_images.checksums_url(url) == "https://cloud-images.ubuntu.com/releases/26.04/release-20260927/SHA256SUMS"
    assert control_images.expected_sha256(CHECKSUMS, "ubuntu-26.04-server-cloudimg-arm64.img").startswith("63a93bd5")
    assert control_images.expected_sha256(CHECKSUMS, "nope.img") is None


def test_cloud_image_path_and_seed_values(home):
    versions = config.load_versions()
    path = control_images.cloud_image_path(versions, "aarch64")
    assert path.parent == home / "downloads"
    assert path.name == f"ubuntu-{versions.ubuntu.release}-{versions.ubuntu.image_serial}-aarch64.img"
    values = control_images.seed_values(config.UserConfig(), PUBKEY)
    assert values["ADDRESS"] == "192.168.64.19"
    assert values["CONTROL_PUBKEY"] == PUBKEY
    assert values["HOST_KEY_PUBLIC"] == render.HOST_KEY_DIGEST_VALUES["HOST_KEY_PUBLIC"]
    private, public = remote.generate_host_key("x")
    real = control_images.seed_values(config.UserConfig(), PUBKEY, render.host_key_values(private, public))
    document = yaml.safe_load(render.cloudinit_document("backend", "cloudinit-image", "user-data", real))
    assert document["ssh_keys"]["ed25519_public"] == public
    assert document["ssh_keys"]["ed25519_private"] == private


def test_image_user_data_and_network_config_render_for_the_build(home):
    values = control_images.seed_values(config.UserConfig(), PUBKEY)
    user_data = render.cloudinit_document("backend", "cloudinit-image", "user-data", values)
    assert user_data.startswith("#cloud-config\n")
    document = yaml.safe_load(user_data)
    assert document["hostname"] == control_images.IMAGE_HOSTNAME
    assert PUBKEY in user_data
    network = yaml.safe_load(render.cloudinit_document("backend", "cloudinit", "network-config", values))
    assert network["ethernets"]["enp0s1"]["addresses"] == ["192.168.64.19/24"]
    with pytest.raises(render.RenderError):
        render.cloudinit_document("backend", "cloudinit-nope", "user-data", values)


def test_write_seed(tmp_path):
    written = render.write_seed(tmp_path / "seed", "#cloud-config\n{}\n", "backend-image-20261108", "backend-image",
                                "version: 2\n")
    assert sorted(path.name for path in written) == ["meta-data", "network-config", "user-data"]
    meta = yaml.safe_load((tmp_path / "seed" / "meta-data").read_text())
    assert meta == {"instance-id": "backend-image-20261108", "local-hostname": "backend-image"}
    without_network = render.write_seed(tmp_path / "seed2", "#cloud-config\n", "i", "h")
    assert len(without_network) == 2


def test_record_round_trip_and_rebuild_reasons(home):
    record = wanted()
    assert control_images.rebuild_reasons(None, record) == ["no image yet"]
    images = config.images_dir()
    images.mkdir()
    control_images.record_path(record.name).write_text(record.to_yaml())
    assert control_images.rebuild_reasons(control_images.load_record(record.name), record)[0].startswith("record without an image")
    assert control_images.rebuild_reasons(control_images.load_record(record.name), record, exists=True) == []
    control_images.disk_path(record.name).write_bytes(b"")
    assert control_images.rebuild_reasons(control_images.load_record(record.name), record) == []
    changed = record.model_copy(update={"ide_backend_version": "9.9.9", "cloudinit_sha256": "0" * 64, "package_sha256": "1" * 64})
    reasons = control_images.rebuild_reasons(control_images.load_record(record.name), changed)
    assert any(reason.startswith("ide_backend_version") for reason in reasons)
    assert "the image cloud-init changed" in reasons
    assert "the package code changed" in reasons
    assert len(record.package_sha256) == 64
    assert control_images.package_digest() == record.package_sha256
    loaded = control_images.load_record(record.name)
    assert loaded.inputs() == record.inputs()
    assert loaded.built is None


def test_ls_and_rm(home, capsys):
    status = cli.main(["image", "ls"])
    assert status == 0
    assert "no base images" in capsys.readouterr().out
    record = wanted()
    record.built = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.UTC)
    config.images_dir().mkdir()
    control_images.record_path(record.name).write_text(record.to_yaml())
    control_images.disk_path(record.name).write_bytes(b"x" * 1024)
    control_images.disk_path("backend-0.0.9").write_bytes(b"")
    assert cli.main(["image", "ls"]) == 0
    out = capsys.readouterr().out
    assert record.name in out
    assert "backend-0.0.9" in out
    assert "2026-11-08 12:00" in out
    assert cli.main(["--dry-run", "image", "rm", record.name]) == 0
    assert control_images.disk_path(record.name).exists()
    assert cli.main(["image", "rm", f"{record.name}.img"]) == 0
    assert not control_images.disk_path(record.name).exists()
    assert not control_images.record_path(record.name).exists()
    assert cli.main(["image", "rm", "backend-0.0.9"]) == 0
    assert cli.main(["image", "rm", "backend-0.0.9"]) == 1


def test_build_dry_run_prints_the_plan(home, monkeypatch, capsys):
    monkeypatch.setattr(control_driver, "load_driver", lambda runner=None: FakeDriver())
    status = cli.main(["--dry-run", "image", "build", "--cpus", "2", "--memory-gib", "4"])
    assert status == 0
    out = capsys.readouterr().out
    assert "would build" in out
    assert "no image yet" in out
    assert "idesbx-image-build" in out
    assert "2 CPUs and 4 GiB" in out
    assert "192.168.64.19" in out
    assert "cloud-init clean" in out


def test_build_is_a_no_op_when_the_image_is_current(home, monkeypatch, capsys):
    monkeypatch.setattr(control_driver, "load_driver", lambda runner=None: FakeDriver())
    (home / "secrets").mkdir()
    (home / "secrets" / "control_ed25519").write_text("private")
    (home / "secrets" / "control_ed25519.pub").write_text(PUBKEY + "\n")
    (home / "secrets" / "sandbox_ed25519").write_text("private")
    (home / "secrets" / "sandbox_ed25519.pub").write_text(PUBKEY + "\n")
    record = control_images.wanted_record(
        config.load_versions(), config.UserConfig(), common.architecture(),
        render.cloudinit_document("backend", "cloudinit-image", "user-data",
                                  control_images.seed_values(config.UserConfig(), PUBKEY)),
        "fake")
    config.images_dir().mkdir()
    control_images.record_path(record.name).write_text(record.to_yaml())
    control_images.disk_path(record.name).write_bytes(b"")
    assert cli.main(["image", "build"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert cli.main(["--dry-run", "image", "build", "--force"]) == 0
    assert "--force" in capsys.readouterr().out


def test_build_reports_a_missing_driver(home, monkeypatch, capsys):
    def no_driver(runner=None):
        raise control_driver.DriverError("no hypervisor here")

    monkeypatch.setattr(control_driver, "load_driver", no_driver)
    assert cli.main(["image", "build"]) == 1
    assert "no hypervisor here" in capsys.readouterr().err


def test_prepare_disk_needs_qemu_img_and_grows_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(control_driver.DriverError) as raised:
        control_images.prepare_disk(tmp_path / "c.img", tmp_path / "d.img", 1, common.Runner())
    assert "brew install qemu" in str(raised.value)

    commands = []

    class Converter:
        dry_run = False
        verbose = False

        def run(self, command, **kwargs):
            commands.append(command)
            pathlib.Path(command[-1]).write_bytes(b"raw")
            retval = subprocess.CompletedProcess(command, 0, "", "")
            return retval

    monkeypatch.setattr(shutil, "which", lambda name: "/opt/homebrew/bin/qemu-img")
    control_images.prepare_disk(tmp_path / "c.img", tmp_path / "d.img", 1, Converter())
    assert commands[0][:5] == ["/opt/homebrew/bin/qemu-img", "convert", "-f", "qcow2", "-O"]
    assert (tmp_path / "d.img").stat().st_size == 1 << 30


def test_warning_colours_only_where_wanted(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv(common.COLOR_ENV_VAR, "always")
    assert common.warning("careful") == f"{common.BOLD_YELLOW}careful{common.RESET}"
    monkeypatch.setenv(common.COLOR_ENV_VAR, "never")
    assert common.warning("careful") == "careful"
    monkeypatch.delenv(common.COLOR_ENV_VAR)
    monkeypatch.setenv("NO_COLOR", "1")
    assert common.color_wanted() is False
    monkeypatch.delenv("NO_COLOR")

    class Tty:
        def isatty(self):
            retval = True
            return retval

    assert common.warning("careful", Tty()).startswith(common.BOLD_YELLOW)


def test_architecture_names_match_versions_yaml(monkeypatch):
    monkeypatch.setattr(common.platform, "machine", lambda: "arm64")
    assert common.architecture() == "aarch64"
    monkeypatch.setattr(common.platform, "machine", lambda: "x86_64")
    assert common.architecture() == "x86_64"
    assert common.format_duration(42.4) == "42 s"
    assert common.format_duration(185) == "3 min 5 s"
