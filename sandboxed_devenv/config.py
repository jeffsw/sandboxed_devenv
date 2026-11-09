"""Where the tool keeps its own state on a machine, and the models of its YAML files.

State lives in one directory, ``~/.sandboxed_devenv/`` (PLAN.md, "Open decisions", state dir,
decided 7 November): ``config.yaml``, ``inventory/`` with one JSON file per project, and
``secrets/`` (mode 0700) with one file per secret and none of them in ``config.yaml``, so
configuration and secrets can be managed separately. ``SANDBOXED_DEVENV_HOME`` moves the whole
directory, for tests and for a second copy of the tool's state. Under ``sudo`` the directory is
the invoking user's, not root's, so ``sudo sandboxed-devenv host install`` reads the secrets that
user put there.

Four pydantic models: ``UserConfig`` for ``config.yaml``, ``ProjectConfig`` for the
``/etc/sandboxed-devenv/project.yaml`` a project's backend receives from its cloud-init,
``Versions`` for the package's own ``versions.yaml``, and ``Policy`` for ``policy.yaml``, the
egress allowlists and the VM subnet (the package's copy, or ``~/.sandboxed_devenv/policy.yaml``
when the user wrote one, since the uplink interface is the host's own).
"""

import datetime
import ipaddress
import os
import pathlib
import pwd
import shutil
import subprocess
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
# Two key pairs in secrets/ (PLAN.md M3, the key split). The control key is the control
# machine's way into desk and every backend; the sandbox key is the client sandbox's way into
# the backends only (``/home/gw/.ssh/vm_ed25519`` on the client host), so a compromised sandbox
# holds nothing that opens the client host. Before the split one pair, ``gw_ed25519``, served
# both; it becomes the control key when found.
CONTROL_KEY_NAME = "control_ed25519"
SANDBOX_KEY_NAME = "sandbox_ed25519"
LEGACY_RIG_KEY_NAME = "gw_ed25519"
CLAUDE_TOKEN_NAME = "claude_oauth_token"
DEFAULT_VM_NAME_PREFIX = "idesbx-"
ARCHITECTURES = ("aarch64", "x86_64")
# The client host VM on the Mac rig; the Linux laptop is its own client host and has none.
CLIENT_HOST_VM_SUFFIX = "desk"
CLIENT_HOST_HOSTNAME = "desk"
PROJECT_NAME_PATTERN = r"^[A-Za-z0-9_.-]+$"


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
    """Base images on UTM: ``backend-<version>.img``, raw disks that each backend is cloned from,
    each with a ``backend-<version>.yaml`` beside it saying what went into it."""
    retval = home_dir() / "images"
    return retval


def downloads_dir() -> pathlib.Path:
    """Downloaded cloud images, kept so a rebuild of the base image fetches nothing."""
    retval = home_dir() / "downloads"
    return retval


def user_policy_file() -> pathlib.Path:
    """A policy.yaml of the user's own, overriding the package's copy when present."""
    retval = home_dir() / "policy.yaml"
    return retval


# A secret file may hold the secret itself or a 1Password reference, `op://vault/item/field`,
# resolved with the 1Password CLI when the secret is read (PLAN.md M3). References may sit in
# config-managed files; the values never do.
OP_PREFIX = "op://"
OP_COMMAND = "op"
SOURCE_ABSENT = "absent"
SOURCE_FILE = "file"
SOURCE_ONEPASSWORD = "1password"


class SecretError(Exception):
    """A reference that cannot be resolved: no `op`, or `op read` failed."""


def secret_source(name: str) -> tuple[str, str | None]:
    """How a secret is kept: (``absent``|``file``|``1password``, the reference when it is one)."""
    path = secrets_dir() / name
    retval = (SOURCE_ABSENT, None)
    if path.is_file():
        text = path.read_text().strip()
        if text.startswith(OP_PREFIX):
            retval = (SOURCE_ONEPASSWORD, text)
        elif text:
            retval = (SOURCE_FILE, None)
    return retval


def resolve_reference(reference: str) -> str:
    """`op read REFERENCE` through the 1Password CLI, which must be on the PATH and signed in."""
    command = shutil.which(OP_COMMAND)
    if not command:
        raise SecretError(f"{reference} needs the 1Password CLI `op` on the PATH (brew install 1password-cli; `op signin`)")
    completed = subprocess.run([command, "read", reference], capture_output=True, text=True)
    if completed.returncode != 0:
        raise SecretError(f"`op read {reference}` failed with status {completed.returncode}: {completed.stderr.strip()[-200:]}")
    retval = completed.stdout.strip()
    if not retval:
        raise SecretError(f"`op read {reference}` returned nothing")
    return retval


def read_secret(name: str, resolver=None) -> str | None:
    """A secret's value: the file's content, stripped, or the value an `op://` reference in it
    resolves to; None when the file is absent or empty."""
    path = secrets_dir() / name
    retval = None
    if path.is_file():
        text = path.read_text().strip()
        if text.startswith(OP_PREFIX):
            retval = (resolver or resolve_reference)(text)
        elif text:
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
    # Which Claude token a backend gets (PLAN.md M3, the policy knob): `shared`, the one token in
    # secrets/claude_oauth_token for every backend; `per-project`, the token in
    # secrets/claude_oauth_token.<project> and none when that file is absent; `none`, never.
    claude_token: typing.Literal["shared", "per-project", "none"] = "shared"
    # Whether a backend made with --disposable (stranger code) gets a token at all.
    disposable_claude_token: bool = False


class UtmConfig(pydantic.BaseModel):
    """UTM's shared network on the Mac: the Mac is .1, desk is .10, backends take .20 upward."""

    model_config = pydantic.ConfigDict(extra="forbid")

    subnet: str = "192.168.64.0/24"
    gateway: str = "192.168.64.1"
    nameserver: str = "192.168.64.1"
    client_host_address: str = "192.168.64.10"
    address_pool_start: str = "192.168.64.20"
    # The one boot of the base image under `image build`: below the pool, above desk, so a build
    # never collides with a project's backend.
    image_build_address: str = "192.168.64.19"
    # The virtio NIC's name under UTM's Apple backend; the netplan match must be exact.
    interface: str = "enp0s1"


class IncusConfig(pydantic.BaseModel):
    """Incus on the Linux laptop (PLAN.md M5): the storage pool (`dir`, in ``storage_dir`` when
    set, else Incus's own location), the managed bridge the backends join, and the address pool
    on it. ``subnet`` and ``address_pool_start`` default to what the bridge reports and its
    twentieth address, like UTM's."""

    model_config = pydantic.ConfigDict(extra="forbid")

    storage_dir: str | None = None
    pool: str = "sandboxed-devenv"
    network: str = "incusbr0"
    subnet: str | None = None
    address_pool_start: str | None = None


class GitIdentity(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    user_name: str
    user_email: str


class UserConfig(pydantic.BaseModel):
    """``~/.sandboxed_devenv/config.yaml``; every field has a default, so the file is optional."""

    model_config = pydantic.ConfigDict(extra="forbid")

    # `local` when this machine is the client host, else `ssh://user@host` for desk.
    client_host: str = "local"
    vm_name_prefix: str = DEFAULT_VM_NAME_PREFIX
    backend: BackendDefaults = pydantic.Field(default_factory=BackendDefaults)
    utm: UtmConfig = pydantic.Field(default_factory=UtmConfig)
    incus: IncusConfig = pydantic.Field(default_factory=IncusConfig)
    # The identity for commits made inside a backend; absent, `new` reads this machine's global
    # git config.
    git: GitIdentity | None = None

    def vm_name(self, project: str) -> str:
        retval = f"{self.vm_name_prefix}{project}"
        return retval

    def client_host_vm_name(self) -> str:
        retval = self.vm_name(CLIENT_HOST_VM_SUFFIX)
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


class ProjectConfig(pydantic.BaseModel):
    """What a project's backend learns from its cloud-init, root-owned mode 0600 because of the token."""

    model_config = pydantic.ConfigDict(extra="forbid")

    project: str = pydantic.Field(pattern=PROJECT_NAME_PATTERN)
    repositories: list[Repository] = pydantic.Field(default_factory=list)
    git: GitIdentity
    claude_oauth_token: str | None = None
    created: datetime.datetime | None = None
    # The project this backend was copied from (`sandboxed-devenv clone`): its directory under
    # ~dev/proj moves to this project's name at first boot, working trees and all.
    cloned_from: str | None = pydantic.Field(default=None, pattern=PROJECT_NAME_PATTERN)

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


# --- policy.yaml -------------------------------------------------------------------------------


class EgressPolicy(pydantic.BaseModel):
    """What one side may reach beyond the VM subnet. ``cidrs`` become nftables set elements;
    ``hosts`` are recorded for the day the ruleset can follow names (nft refuses a name that
    resolves to several addresses, found 8 November 2026 on desk; M5 decides the mechanism)."""

    model_config = pydantic.ConfigDict(extra="forbid")

    cidrs: list[str] = pydantic.Field(default_factory=list)
    hosts: list[str] = pydantic.Field(default_factory=list)
    ports: list[int] = pydantic.Field(default_factory=lambda: [443])

    @pydantic.field_validator("cidrs")
    @classmethod
    def _valid_cidrs(cls, value: list[str]) -> list[str]:
        for cidr in value:
            ipaddress.ip_network(cidr)
        retval = value
        return retval


class Policy(pydantic.BaseModel):
    """``policy.yaml``: the VM subnet, the client host's uplink, both egress allowlists, and
    whether `host install` enables the nftables service."""

    model_config = pydantic.ConfigDict(extra="forbid")

    vm_subnet: str = "192.168.64.0/24"
    uplink_interface: str = "en*"
    enforce: bool = False
    client: EgressPolicy = pydantic.Field(default_factory=EgressPolicy)
    backend: EgressPolicy = pydantic.Field(default_factory=EgressPolicy)

    @pydantic.field_validator("vm_subnet")
    @classmethod
    def _valid_subnet(cls, value: str) -> str:
        ipaddress.ip_network(value)
        retval = value
        return retval

    def subnet_halves(self) -> tuple[str, str]:
        """The two halves of the VM subnet: on the Mac rig, routes to them through the Mac are
        more specific than desk's on-link route, so every backend is reachable without a
        per-backend change (PLAN.md M2, routing on desk)."""
        network = ipaddress.ip_network(self.vm_subnet)
        lower, upper = network.subnets(prefixlen_diff=1)
        retval = (str(lower), str(upper))
        return retval


def package_policy_file() -> pathlib.Path:
    retval = pathlib.Path(__file__).resolve().parent / "policy.yaml"
    return retval


def policy_file() -> pathlib.Path:
    """The user's policy.yaml when there is one, else the package's."""
    retval = user_policy_file()
    if not retval.is_file():
        retval = package_policy_file()
    return retval


def load_policy() -> Policy:
    retval = Policy(**(yaml.safe_load(policy_file().read_text()) or {}))
    return retval
