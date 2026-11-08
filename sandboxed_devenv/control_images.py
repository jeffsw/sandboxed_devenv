"""`sandboxed-devenv image ...`: the base image every backend is launched from (control role, M2).

`image build` downloads Ubuntu's cloud image (versions.yaml, checked against the published
SHA256SUMS), converts it to a raw disk grown to `disk_gib` (sparse; the filesystem fills it at
first boot), boots it once with the image-stage cloud-init, waits for SSH and for cloud-init,
pushes the wheel and installs it with `sudo pipx install --global`, runs `backend prepare
--stage image`, then `cloud-init clean` so the next boot is a first boot again, powers the VM
off and keeps its disk as ``~/.sandboxed_devenv/images/backend-<package version>.img`` with a
YAML record beside it saying what went in. A project's backend (`new`, PLAN.md M2) is an APFS
clone of that file. Rebuilding happens when any recorded version, or the rendered image
cloud-init, differs from what the package carries now, or with `--force`.

Slow steps say what they do and why before they start and how long they took when they end
(PLAN.md, "Principles"); nothing here draws a progress bar.
"""

import datetime
import hashlib
import os
import pathlib
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import pydantic
import yaml

from sandboxed_devenv import common, config, control_creds, control_driver, remote, render
from sandboxed_devenv.control_driver import DriverError, VmSpec
from sandboxed_devenv.remote import RemoteError
from sandboxed_devenv.render import RenderError

COMMAND = "image build"
IMAGE_PREFIX = "backend-"
DISK_SUFFIX = ".img"
RECORD_SUFFIX = ".yaml"
BUILD_DIR_PREFIX = ".build-"
# The build VM's name takes the user's prefix like every backend, so `utmctl list` groups it.
BUILD_VM_SUFFIX = "image-build"
IMAGE_HOSTNAME = "backend-image"
CHECKSUMS_FILE = "SHA256SUMS"
DOWNLOAD_CHUNK = 1 << 20
# First login: the users module runs early in boot, so a few minutes covers a slow Mac.
SSH_TIMEOUT = 600
# cloud-init on the image stage downloads the IDE backend (about a gigabyte) and the tools.
CLOUD_INIT_TIMEOUT = 2400
POWEROFF_TIMEOUT = 180
LEFTOVER_STOP_TIMEOUT = 60


class ImageRecord(pydantic.BaseModel):
    """``backend-<version>.yaml`` beside the disk: what the image was built from and when."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str
    driver: str
    architecture: str
    package_version: str
    # Of the package's source tree as installed here: a code change rebuilds the image even
    # when the version did not move (found 8 November 2026, when a backend ran a canary older
    # than the control side's).
    package_sha256: str = ""
    ide_backend_version: str
    ubuntu_release: str
    ubuntu_image_serial: str
    # Of the rendered image-stage user-data: a template change rebuilds the image too.
    cloudinit_sha256: str
    disk_gib: int
    size_bytes: int = 0
    allocated_bytes: int = 0
    built: datetime.datetime | None = None

    def to_yaml(self) -> str:
        retval = yaml.safe_dump(self.model_dump(mode="json", exclude_none=True), sort_keys=False)
        return retval

    def inputs(self) -> dict:
        """The fields a rebuild decision compares."""
        retdict = {
            "architecture": self.architecture,
            "package_version": self.package_version,
            "package_sha256": self.package_sha256,
            "ide_backend_version": self.ide_backend_version,
            "ubuntu_release": self.ubuntu_release,
            "ubuntu_image_serial": self.ubuntu_image_serial,
            "cloudinit_sha256": self.cloudinit_sha256,
            "disk_gib": self.disk_gib,
        }
        return retdict


def register(subparsers):
    image = common.add_group(subparsers, "image", "the base image backends are launched from (control)")

    build = image.add_parser(
        "build",
        help="download the Ubuntu cloud image, boot it with the base cloud-init, install the package, clean, power off, keep the disk",
    )
    build.add_argument("--force", action="store_true", help="rebuild even when the versions have not changed")
    build.add_argument("--wheel", metavar="PATH", help="push this wheel instead of building one from the checkout")
    build.add_argument("--cpus", metavar="N", type=int, help="CPUs for the build VM (default: the backend default)")
    build.add_argument("--memory-gib", metavar="N", type=int, help="memory for the build VM in GiB (default: the backend default)")
    build.set_defaults(handler=cmd_build)

    ls = image.add_parser("ls", help="base images and their versions")
    ls.set_defaults(handler=cmd_ls)

    rm = image.add_parser("rm", help="delete a base image and its record")
    rm.add_argument("image", metavar="IMAGE", help="the image as `image ls` names it")
    rm.set_defaults(handler=cmd_rm)


# --- names and records ---------------------------------------------------------------------------


def image_name(package_version: str) -> str:
    retval = f"{IMAGE_PREFIX}{package_version}"
    return retval


def disk_path(name: str) -> pathlib.Path:
    retval = config.images_dir() / f"{name}{DISK_SUFFIX}"
    return retval


def record_path(name: str) -> pathlib.Path:
    retval = config.images_dir() / f"{name}{RECORD_SUFFIX}"
    return retval


def normalize_name(given: str) -> str:
    """`image rm` accepts the name with or without the disk suffix."""
    retval = given
    if retval.endswith(DISK_SUFFIX):
        retval = retval[: -len(DISK_SUFFIX)]
    return retval


def load_record(name: str) -> ImageRecord | None:
    path = record_path(name)
    retval = None
    if path.is_file():
        retval = ImageRecord(**(yaml.safe_load(path.read_text()) or {}))
    return retval


def list_images() -> list[ImageRecord]:
    """Every disk under the images directory with its record, oldest first; a disk without a
    record is listed with what the file alone says."""
    retlist = []
    images = config.images_dir()
    if images.is_dir():
        for disk in sorted(images.glob(f"{IMAGE_PREFIX}*{DISK_SUFFIX}")):
            name = disk.name[: -len(DISK_SUFFIX)]
            record = load_record(name)
            if record is None:
                record = ImageRecord(name=name, driver="?", architecture="?", package_version="?",
                                     ide_backend_version="?", ubuntu_release="?", ubuntu_image_serial="?",
                                     cloudinit_sha256="", disk_gib=0)
            status = disk.stat()
            record.size_bytes = status.st_size
            record.allocated_bytes = status.st_blocks * 512
            retlist.append(record)
    return retlist


def package_digest() -> str:
    """A SHA-256 over the package's source tree as installed here (every file under the package
    directory but caches, by relative path and content), so a rebuild decision sees code changes
    that a version bump did not record."""
    root = pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    retval = digest.hexdigest()
    return retval


def wanted_record(versions: config.Versions, user_config: config.UserConfig, architecture: str,
                  user_data: str, driver_name: str) -> ImageRecord:
    retval = ImageRecord(
        name=image_name(common.package_version()),
        driver=driver_name,
        architecture=architecture,
        package_version=common.package_version(),
        package_sha256=package_digest(),
        ide_backend_version=versions.ide_backend.version,
        ubuntu_release=versions.ubuntu.release,
        ubuntu_image_serial=versions.ubuntu.image_serial,
        cloudinit_sha256=hashlib.sha256(user_data.encode()).hexdigest(),
        disk_gib=user_config.backend.disk_gib,
    )
    return retval


def rebuild_reasons(current: ImageRecord | None, wanted: ImageRecord) -> list[str]:
    """Why the image must be (re)built; empty when the existing one matches."""
    retlist = []
    if current is None:
        retlist.append("no image yet")
    elif not disk_path(wanted.name).is_file():
        retlist.append(f"record without a disk at {disk_path(wanted.name)}")
    else:
        for key, value in wanted.inputs().items():
            have = current.inputs().get(key)
            if have != value:
                if key == "cloudinit_sha256":
                    retlist.append("the image cloud-init changed")
                elif key == "package_sha256":
                    retlist.append("the package code changed")
                else:
                    retlist.append(f"{key} {have} -> {value}")
    return retlist


def format_size(size: int) -> str:
    retval = f"{size / (1 << 30):.1f} GiB" if size >= (1 << 30) else f"{size / (1 << 20):.0f} MiB"
    return retval


# --- the cloud image -----------------------------------------------------------------------------


def cloud_image_path(versions: config.Versions, architecture: str) -> pathlib.Path:
    ubuntu = versions.ubuntu
    retval = config.downloads_dir() / f"ubuntu-{ubuntu.release}-{ubuntu.image_serial}-{architecture}.img"
    return retval


def checksums_url(image_url: str) -> str:
    retval = urllib.parse.urljoin(image_url, CHECKSUMS_FILE)
    return retval


def expected_sha256(checksums_text: str, file_name: str) -> str | None:
    """The digest for one file from an Ubuntu SHA256SUMS listing (`<digest> *<name>`)."""
    retval = None
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == file_name:
            retval = parts[0]
            break
    return retval


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    retval = digest.hexdigest()
    return retval


def fetch_text(url: str, timeout: int = 60) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        retval = response.read().decode()
    return retval


def download(url: str, dest: pathlib.Path) -> str:
    """Stream ``url`` to ``dest`` through a temporary file; returns the SHA-256 of what arrived."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=f".{dest.name}.", delete=False) as handle:
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
        except BaseException:
            handle.close()
            os.unlink(handle.name)
            raise
    os.replace(handle.name, dest)
    retval = digest.hexdigest()
    return retval


def ensure_cloud_image(versions: config.Versions, architecture: str) -> pathlib.Path:
    """The cloud image for this architecture under downloads/, fetched when missing and checked
    against Ubuntu's SHA256SUMS either way."""
    url = versions.ubuntu.url(architecture)
    retval = cloud_image_path(versions, architecture)
    file_name = url.rsplit("/", 1)[-1]
    try:
        expected = expected_sha256(fetch_text(checksums_url(url)), file_name)
    except OSError as error:
        expected = None
        if not retval.is_file():
            raise RemoteError(f"cannot fetch {checksums_url(url)}: {error}") from error
        print(f"{COMMAND}: cloud image: {checksums_url(url)} unreachable ({error}); using the existing file unchecked",
              flush=True)
    if retval.is_file():
        with common.slow_step(COMMAND, "cloud image", f"checking {retval} against {CHECKSUMS_FILE}"):
            actual = sha256_of(retval)
        if expected is not None and actual != expected:
            print(f"{COMMAND}: cloud image: {retval} does not match {CHECKSUMS_FILE}; fetching it again", flush=True)
            retval.unlink()
    if not retval.is_file():
        with common.slow_step(COMMAND, "cloud image", f"downloading {url} (about a gigabyte) to {retval}"):
            actual = download(url, retval)
        if expected is not None and actual != expected:
            retval.unlink()
            raise RemoteError(f"{url} does not match {CHECKSUMS_FILE}: got {actual}, expected {expected}")
    return retval


def prepare_disk(cloud_image: pathlib.Path, disk: pathlib.Path, disk_gib: int, runner: common.Runner) -> None:
    """The raw disk UTM's Apple backend accepts: qemu-img converts the QCOW2 cloud image, then the
    file is grown to `disk_gib`, sparse; cloud-init grows the partition and filesystem at first boot."""
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise DriverError("qemu-img is not on the PATH; `brew install qemu` provides it (the cloud image is QCOW2 and "
                          "UTM's Apple backend takes raw disks only)")
    disk.parent.mkdir(parents=True, exist_ok=True)
    completed = runner.run([qemu_img, "convert", "-f", "qcow2", "-O", "raw", str(cloud_image), str(disk)],
                           capture_output=True)
    if completed.returncode != 0:
        raise DriverError(f"qemu-img convert failed with status {completed.returncode}: {(completed.stderr or '').strip()}")
    if not runner.dry_run:
        os.truncate(disk, disk_gib << 30)


# --- the build -----------------------------------------------------------------------------------


def seed_values(user_config: config.UserConfig, public_key: str, host_key: dict[str, str] | None = None) -> dict[str, str]:
    """The image seed's values: the control key for dev, the build address, and the host key pair,
    or the fixed digest values when the document is rendered only to be hashed."""
    retdict = render.default_values(user_config, CONTROL_PUBKEY=public_key, ADDRESS=user_config.utm.image_build_address)
    retdict.update(host_key or render.HOST_KEY_DIGEST_VALUES)
    return retdict


def build_vm_name(user_config: config.UserConfig) -> str:
    retval = user_config.vm_name(BUILD_VM_SUFFIX)
    return retval


def plan_lines(wanted: ImageRecord, reasons: list[str], user_config: config.UserConfig, driver_name: str,
               cloud_image: pathlib.Path, wheel: str, cpus: int, memory_gib: int) -> list[str]:
    vm = build_vm_name(user_config)
    address = user_config.utm.image_build_address
    retlist = [
        f"would build {disk_path(wanted.name)} with the {driver_name} driver: " + "; ".join(reasons),
        f"would fetch {cloud_image.name} into {cloud_image.parent} unless present, checked against {CHECKSUMS_FILE}",
        f"would convert it to a raw disk of {wanted.disk_gib} GiB (sparse) with qemu-img",
        f"would render the image-stage seed (hostname {IMAGE_HOSTNAME}, address {address}) and create VM {vm} "
        f"with {cpus} CPUs and {memory_gib} GiB",
        f"would start {vm}, wait for SSH as dev at {address}, then for cloud-init",
        f"would push {wheel}, run `sudo pipx install --global` and `sudo sandboxed-devenv backend prepare --stage image`",
        "would run `sudo cloud-init clean --logs --machine-id --configs network` and power off",
        f"would keep the disk as {disk_path(wanted.name)} with {record_path(wanted.name)}, then delete {vm}",
    ]
    return retlist


def remove_leftover(driver, vm: str) -> None:
    """A build VM from an earlier failed run: stop it if needed and delete it."""
    if driver.exists(vm):
        state = driver.state(vm)
        print(common.warning(f"{COMMAND}: removing the leftover build VM {vm} ({state})"), flush=True)
        if state != control_driver.STOPPED:
            try:
                driver.stop(vm, force=False)
                control_driver.wait_for_state(driver, vm, control_driver.STOPPED, LEFTOVER_STOP_TIMEOUT)
            except DriverError:
                driver.stop(vm, force=True)
                control_driver.wait_for_state(driver, vm, control_driver.STOPPED, LEFTOVER_STOP_TIMEOUT)
        driver.delete(vm)


def provision(backend: remote.Remote, wheel: pathlib.Path) -> dict:
    """Everything that happens inside the booted build VM; returns its final status report."""
    with common.slow_step(COMMAND, "first boot", f"waiting for SSH as {backend.target}; the users module runs early in boot"):
        backend.wait_for_ssh(SSH_TIMEOUT)
    with common.slow_step(COMMAND, "cloud-init", "waiting for the image stage: apt packages, the IDE backend tarball "
                          "(about a gigabyte), its plugins, Claude Code, uv and mise; several minutes"):
        backend.wait_for_cloud_init(CLOUD_INIT_TIMEOUT)
    with common.slow_step(COMMAND, "package", f"pushing {wheel.name} and running sudo pipx install --global"):
        version = backend.install_wheel(wheel)
    print(f"{COMMAND}: package: {version}", flush=True)
    backend.tool(["backend", "prepare", "--stage", "image"], sudo=True)
    retval = backend.status()
    ide = retval.get("ide_backend") or {}
    if not backend.runner.dry_run and not (ide.get("installed") and ide.get("registered")):
        raise RemoteError(f"the IDE backend is not installed and registered in the image: {ide}; "
                          "see the cloud-init output on the VM (/var/log/cloud-init-output.log)")
    backend.run(["sudo", "cloud-init", "clean", "--logs", "--machine-id", "--configs", "network"])
    backend.run(["sudo", "systemctl", "poweroff"], check=False, quiet=True)
    return retval


def build(args) -> int:
    runner = common.Runner(dry_run=args.dry_run, verbose=args.verbose)
    user_config = config.load_user_config()
    versions = config.load_versions()
    architecture = common.architecture()
    driver = control_driver.load_driver(runner)
    cpus = args.cpus or user_config.backend.cpus
    memory_gib = args.memory_gib or user_config.backend.memory_gib
    public_key = control_creds.seed_key_values(args.dry_run, runner)["CONTROL_PUBKEY"]
    values = seed_values(user_config, public_key)
    digest_user_data = render.cloudinit_document("backend", "cloudinit-image", "user-data", values)
    network_config = render.cloudinit_document("backend", "cloudinit", "network-config", values)
    wanted = wanted_record(versions, user_config, architecture, digest_user_data, driver.name)
    reasons = rebuild_reasons(load_record(wanted.name), wanted)
    if args.force:
        reasons.append("--force")
    if not reasons:
        print(f"{COMMAND}: {disk_path(wanted.name)} is up to date (package {wanted.package_version}, IDE backend "
              f"{wanted.ide_backend_version}, Ubuntu {wanted.ubuntu_release} serial {wanted.ubuntu_image_serial}); "
              "--force rebuilds it")
        retval = 0
        return retval
    cloud_image = cloud_image_path(versions, architecture)
    if args.dry_run:
        wheel_text = args.wheel or "the wheel built from the checkout"
        for line in plan_lines(wanted, reasons, user_config, driver.name, cloud_image, wheel_text, cpus, memory_gib):
            print(line)
        retval = 0
        return retval

    started = time.monotonic()
    print(f"{COMMAND}: building {wanted.name} ({'; '.join(reasons)})", flush=True)
    images = config.images_dir()
    build_dir = images / f"{BUILD_DIR_PREFIX}{wanted.name}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    if args.wheel:
        wheel = pathlib.Path(args.wheel)
        if not wheel.is_file():
            raise RenderError(f"no wheel at {wheel}")
    else:
        checkout = render.find_checkout()
        if checkout is None:
            raise RenderError("run `image build` inside a checkout of the repository, or pass --wheel PATH")
        wheel = render.build_wheel(checkout, build_dir, runner)
    cloud_image = ensure_cloud_image(versions, architecture)
    disk = build_dir / f"{wanted.name}{DISK_SUFFIX}"
    with common.slow_step(COMMAND, "raw disk", f"converting {cloud_image.name} to {disk} and growing it to "
                          f"{wanted.disk_gib} GiB (sparse: the filesystem fills it at first boot)"):
        prepare_disk(cloud_image, disk, wanted.disk_gib, runner)
    seed_dir = build_dir / "seed"
    instance_id = f"{IMAGE_HOSTNAME}-{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d%H%M%S')}"
    host_private, host_public = remote.generate_host_key(IMAGE_HOSTNAME)
    user_data = render.cloudinit_document("backend", "cloudinit-image", "user-data",
                                          seed_values(user_config, public_key, render.host_key_values(host_private, host_public)))
    render.write_seed(seed_dir, user_data, instance_id, IMAGE_HOSTNAME, network_config)

    vm = build_vm_name(user_config)
    remove_leftover(driver, vm)
    remote.forget_host_keys(vm)
    remote.pin_host_key(vm, user_config.utm.image_build_address, host_public)
    with common.slow_step(COMMAND, "create", f"UTM copies the {wanted.disk_gib} GiB disk and the seed ISO into the bundle of {vm}"):
        driver.create(VmSpec(name=vm, disk=disk, cpus=cpus, memory_gib=memory_gib, seed_dir=seed_dir))
    disk.unlink()
    driver.start(vm)
    backend = remote.backend(user_config.utm.image_build_address, vm, runner)
    try:
        provision(backend, wheel)
        with common.slow_step(COMMAND, "power off", f"waiting for {vm} to stop"):
            try:
                control_driver.wait_for_state(driver, vm, control_driver.STOPPED, POWEROFF_TIMEOUT)
            except DriverError:
                print(common.warning(f"{COMMAND}: power off: {vm} did not stop by itself; forcing it"), flush=True)
                driver.stop(vm, force=True)
                control_driver.wait_for_state(driver, vm, control_driver.STOPPED, LEFTOVER_STOP_TIMEOUT)
    except (RemoteError, DriverError) as error:
        print(f"{COMMAND}: failed: {error}", file=sys.stderr)
        print(f"{COMMAND}: the build VM {vm} is left as it is for inspection: `utmctl status {vm}`, "
              f"`ssh -i {remote.control_key_path()} -o IdentitiesOnly=yes dev@{user_config.utm.image_build_address}`, "
              f"`utmctl delete {vm}`; the next `image build` removes it", file=sys.stderr)
        shutil.rmtree(build_dir, ignore_errors=True)
        retval = 1
        return retval

    images.mkdir(parents=True, exist_ok=True)
    final_disk = disk_path(wanted.name)
    temporary = final_disk.with_name(f"{final_disk.name}.tmp")
    with common.slow_step(COMMAND, "export", f"cloning {vm}'s disk out of its bundle to {final_disk}"):
        driver.export_disk(vm, temporary)
        os.replace(temporary, final_disk)
    status = final_disk.stat()
    wanted.size_bytes = status.st_size
    wanted.allocated_bytes = status.st_blocks * 512
    wanted.built = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    record_path(wanted.name).write_text(wanted.to_yaml())
    driver.delete(vm)
    remote.forget_host_keys(vm)
    shutil.rmtree(build_dir, ignore_errors=True)
    print(f"{COMMAND}: {final_disk} ready: {format_size(wanted.allocated_bytes)} allocated of "
          f"{format_size(wanted.size_bytes)}, IDE backend {wanted.ide_backend_version}, Ubuntu {wanted.ubuntu_release} "
          f"serial {wanted.ubuntu_image_serial}, package {wanted.package_version}; total "
          f"{common.format_duration(time.monotonic() - started)}")
    retval = 0
    return retval


def cmd_build(args) -> int:
    try:
        retval = build(args)
    except (RemoteError, DriverError, RenderError, OSError) as error:
        retval = remote.report(error, COMMAND)
    return retval


def cmd_ls(args) -> int:
    records = list_images()
    if not records:
        print(f"no base images under {config.images_dir()}; `sandboxed-devenv image build` makes one")
    else:
        rows = [("IMAGE", "ARCH", "IDE BACKEND", "UBUNTU", "PACKAGE", "BUILT", "SIZE")]
        for record in records:
            built = record.built.strftime("%Y-%m-%d %H:%M") if record.built else "?"
            size = f"{format_size(record.allocated_bytes)} of {format_size(record.size_bytes)}"
            rows.append((record.name, record.architecture, record.ide_backend_version,
                         f"{record.ubuntu_release} {record.ubuntu_image_serial}", record.package_version, built, size))
        widths = [0] * len(rows[0])
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        for row in rows:
            print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    retval = 0
    return retval


def cmd_rm(args) -> int:
    name = normalize_name(args.image)
    disk = disk_path(name)
    record = record_path(name)
    if not disk.exists() and not record.exists():
        print(f"sandboxed-devenv image rm: no image named {name} under {config.images_dir()}", file=sys.stderr)
        retval = 1
        return retval
    for path in (disk, record):
        if path.exists():
            if args.dry_run:
                print(f"would remove {path}")
            else:
                path.unlink()
                print(f"removed {path}")
    retval = 0
    return retval
