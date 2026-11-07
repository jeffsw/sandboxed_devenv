"""Where the tool keeps its own state on a machine, and the models of its YAML files.

State lives in one directory, ``~/.sandboxed_devenv/`` (PLAN.md, "Open decisions", state dir,
decided 7 November): ``config.yaml``, ``inventory/`` with one JSON file per project, and
``secrets/`` (mode 0700) with one file per secret and none of them in ``config.yaml``, so
configuration and secrets can be managed separately. ``SANDBOXED_DEVENV_HOME`` moves the whole
directory, for tests and for a second copy of the tool's state. Under ``sudo`` the directory is
the invoking user's, not root's, so ``sudo sandboxed-devenv host install`` reads the secrets that
user put there.

Three pydantic models: ``UserConfig`` for ``config.yaml``, ``ProjectConfig`` for the
``/etc/sandboxed-devenv/project.yaml`` a project's backend receives from its cloud-init, and
``Versions`` for the package's own ``versions.yaml``.
"""

import datetime
import os
import pathlib
import pwd
import typing

import pydantic
import yaml

HOME_DIR_NAME = ".sandboxed_devenv"
HOME_ENV_VAR = "SANDBOXED_DEVENV_HOME"
# Inside a backend VM. project.yaml marks the backend role; image.yaml says which base image
# the VM was built from; prepared.yaml says `backend prepare` finished the project stage.
BACKEND_CONFIG_DIR = pathlib.Path("/etc/sandboxed-devenv")
BACKEND_PROJECT_CONFIG = BACKEND_CONFIG_DIR / "project.yaml"
BACKEND_IMAGE_MARKER = BACKEND_CONFIG_DIR / "image.yaml"
BACKEND_PREPARED_MARKER = BACKEND_CONFIG_DIR / "prepared.yaml"
# The rig key pair in secrets/: the control machine's way into desk and every backend, and the
# client sandbox's key to the backends (``/home/gw/.ssh/vm_ed25519`` on the client host).
RIG_KEY_NAME = "gw_ed25519"
CLAUDE_TOKEN_NAME = "claude_oauth_token"
DEFAULT_VM_NAME_PREFIX = "idesbx-"
ARCHITECTURES = ("aarch64", "x86_64")


def invoking_user_home() -> pathlib.Path:
    """The home of whoever ran the command: under sudo, ``SUDO_USER``'s home rather than root's."""
    retval = pathlib.Path.home()
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            retval = pathlib.Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return retval


def home_dir() -> pathlib.Path:
    """``~/.sandboxed_devenv/`` of the invoking user, or ``$SANDBOXED_DEVENV_HOME`` when set."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        retval = pathlib.Path(override)
    else:
        retval = invoking_user_home() / HOME_DIR_NAME
    return retval


def config_file() -> pathlib.Path:
    """The user config: client host, VM name prefix, backend defaults; never a secret value."""
    retval = home_dir() / "config.yaml"
    return retval


def inventory_dir() -> pathlib.Path:
    """One JSON file per project: its backend and every credential (PLAN.md M3)."""
    retval = home_dir() / "inventory"
    return retval


def secrets_dir() -> pathlib.Path:
    """The rig key pair and the Claude token, one file each, mode 0700 (PLAN.md M3)."""
    retval = home_dir() / "secrets"
    return retval


def images_dir() -> pathlib.Path:
    """Base images on UTM: ``backend-<version>.img``, raw disks that each backend is cloned from."""
    retval = home_dir() / "images"
    return retval


def read_secret(name: str) -> str | None:
    """A secret's content, stripped, or None when the file is absent or empty."""
    path = secrets_dir() / name
    retval = None
    if path.is_file():
        text = path.read_text().strip()
        if text:
            retval = text
    return retval


# --- config.yaml ------------------------------------------------------------------------------


class BackendDefaults(pydantic.BaseModel):
    """Resources a new backend gets unless `new` says otherwise; `resources` changes them later."""

    model_config = pydantic.ConfigDict(extra="forbid")

    cpus: int = 4
    memory_gib: int = 8
    # The base image is grown to this size, sparse; the filesystem fills it at first boot.
    disk_gib: int = 32


class UtmConfig(pydantic.BaseModel):
    """UTM's shared network on the Mac: the Mac is .1, desk is .10, backends take .20 upward."""

    model_config = pydantic.ConfigDict(extra="forbid")

    subnet: str = "192.168.64.0/24"
    gateway: str = "192.168.64.1"
    nameserver: str = "192.168.64.1"
    client_host_address: str = "192.168.64.10"
    address_pool_start: str = "192.168.64.20"
    # The virtio NIC's name under UTM's Apple backend; the netplan match must be exact.
    interface: str = "enp0s1"


class IncusConfig(pydantic.BaseModel):
    """The Incus `dir` pool's directory; None means Incus's own default location."""

    model_config = pydantic.ConfigDict(extra="forbid")

    storage_dir: str | None = None


class UserConfig(pydantic.BaseModel):
    """``~/.sandboxed_devenv/config.yaml``; every field has a default, so the file is optional."""

    model_config = pydantic.ConfigDict(extra="forbid")

    # `local` when this machine is the client host, else `ssh://user@host` for desk.
    client_host: str = "local"
    vm_name_prefix: str = DEFAULT_VM_NAME_PREFIX
    backend: BackendDefaults = pydantic.Field(default_factory=BackendDefaults)
    utm: UtmConfig = pydantic.Field(default_factory=UtmConfig)
    incus: IncusConfig = pydantic.Field(default_factory=IncusConfig)

    def vm_name(self, project: str) -> str:
        retval = f"{self.vm_name_prefix}{project}"
        return retval


def load_user_config() -> UserConfig:
    """config.yaml parsed and validated, or the defaults when there is no file."""
    path = config_file()
    if path.is_file():
        data = yaml.safe_load(path.read_text()) or {}
        retval = UserConfig(**data)
    else:
        retval = UserConfig()
    return retval


# --- /etc/sandboxed-devenv/project.yaml ------------------------------------------------------


class Repository(pydantic.BaseModel):
    """One GitHub repository of a project; the key is minted inside the VM by `backend repo add`."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str = pydantic.Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", description="OWNER/REPO")
    access: typing.Literal["read", "write"] = "read"

    @property
    def owner(self) -> str:
        retval = self.name.split("/", 1)[0]
        return retval

    @property
    def repo(self) -> str:
        retval = self.name.split("/", 1)[1]
        return retval


class GitIdentity(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    user_name: str
    user_email: str


class ProjectConfig(pydantic.BaseModel):
    """What a project's backend learns from its cloud-init, root-owned mode 0600 because of the token."""

    model_config = pydantic.ConfigDict(extra="forbid")

    project: str = pydantic.Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repositories: list[Repository] = pydantic.Field(default_factory=list)
    git: GitIdentity
    claude_oauth_token: str | None = None
    created: datetime.datetime | None = None

    def to_yaml(self) -> str:
        retval = yaml.safe_dump(self.model_dump(mode="json", exclude_none=True), sort_keys=False)
        return retval


def load_project_config(path: pathlib.Path = BACKEND_PROJECT_CONFIG) -> ProjectConfig | None:
    """The backend's project.yaml, or None off a backend."""
    retval = None
    if path.is_file():
        retval = ProjectConfig(**(yaml.safe_load(path.read_text()) or {}))
    return retval


# --- versions.yaml -----------------------------------------------------------------------------


class Download(pydantic.BaseModel):
    """URLs keyed by architecture as `uname -m` names it."""

    model_config = pydantic.ConfigDict(extra="forbid")

    aarch64: str
    x86_64: str

    def url(self, architecture: str, **fields: str) -> str:
        if architecture not in ARCHITECTURES:
            raise ValueError(f"no download for architecture {architecture!r}; known: {', '.join(ARCHITECTURES)}")
        retval = getattr(self, architecture).format(**fields)
        return retval


class GatewayVersion(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    version: str
    download: Download

    @property
    def branch(self) -> str:
        """The first two components of the version: Gateway's settings directory carries the branch."""
        retval = ".".join(self.version.split(".")[:2])
        return retval

    def url(self, architecture: str) -> str:
        retval = self.download.url(architecture, version=self.version)
        return retval


class IdeBackendVersion(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    product: str
    version: str
    download: Download
    plugins: list[str] = pydantic.Field(default_factory=list)

    def url(self, architecture: str) -> str:
        retval = self.download.url(architecture, version=self.version)
        return retval


class ClientVersion(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    build: str


class UbuntuVersion(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    release: str
    codename: str
    image_serial: str
    cloud_image: Download

    def url(self, architecture: str) -> str:
        retval = self.cloud_image.url(architecture, serial=self.image_serial)
        return retval


class Versions(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    gateway: GatewayVersion
    ide_backend: IdeBackendVersion
    client: ClientVersion
    ubuntu: UbuntuVersion


def versions_file() -> pathlib.Path:
    retval = pathlib.Path(__file__).resolve().parent / "versions.yaml"
    return retval


def load_versions() -> Versions:
    retval = Versions(**yaml.safe_load(versions_file().read_text()))
    return retval
