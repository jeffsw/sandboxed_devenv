"""The inventory: one JSON file per project under ``~/.sandboxed_devenv/inventory/`` (control role).

A project's record names its backend (the VM, the driver, the base image and package version
it was launched from, its address and resources), the repositories with their key fingerprints
and GitHub ids, and every other credential with how it is revoked (PLAN.md, M3). `new` writes the
backend side and the repositories as the backend minted them; control_creds.py fills in the
registration, revocation and rotation, and keeps a record whose backend is gone while a
revocation is still pending.
Every control command that needs a backend finds it here by the project's name; no command
takes a VM name.
"""

import datetime
import ipaddress
import json
import pathlib
import re
import socket
import typing

import pydantic

from sandboxed_devenv import config

RECORD_SUFFIX = ".json"
# A repository's deploy key: minted in the VM but not yet on GitHub (M2), registered, or
# waiting for a revocation that failed.
UNREGISTERED = "unregistered"
ACTIVE = "active"
PENDING_REVOKE = "pending-revoke"
PROBE_TIMEOUT = 1.0


class InventoryError(Exception):
    pass


class RepositoryRecord(pydantic.BaseModel):
    """One repository of a project and the deploy key that serves it."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str
    access: typing.Literal["read", "write"] = "read"
    checkout: str | None = None
    fingerprint: str | None = None
    public_key: str | None = None
    github_key_id: int | None = None
    title: str | None = None
    state: typing.Literal["unregistered", "active", "pending-revoke"] = UNREGISTERED
    created: datetime.datetime | None = None
    registered: datetime.datetime | None = None


class CredentialRecord(pydantic.BaseModel):
    """Everything that is not a deploy key: the Claude token, the rig key, a git identity."""

    model_config = pydantic.ConfigDict(extra="forbid")

    kind: str
    scope: str
    fingerprint: str | None = None
    location: str
    revoke: str
    state: str = ACTIVE
    created: datetime.datetime | None = None


class ProjectRecord(pydantic.BaseModel):
    """``inventory/<project>.json``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    project: str = pydantic.Field(pattern=config.PROJECT_NAME_PATTERN)
    vm_name: str
    driver: str
    image: str
    package_version: str
    address: str | None = None
    cpus: int
    memory_gib: int
    disk_gib: int
    disposable: bool = False
    cloned_from: str | None = None
    created: datetime.datetime | None = None
    # Set when the backend was deleted but a revocation failed: the record stays for `creds audit`.
    removed: datetime.datetime | None = None
    repos: list[RepositoryRecord] = pydantic.Field(default_factory=list)
    credentials: list[CredentialRecord] = pydantic.Field(default_factory=list)

    def to_json(self) -> str:
        retval = json.dumps(self.model_dump(mode="json"), indent=2) + "\n"
        return retval


def now() -> datetime.datetime:
    retval = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    return retval


def valid_project_name(name: str) -> bool:
    retval = re.fullmatch(config.PROJECT_NAME_PATTERN, name) is not None
    return retval


def record_path(project: str) -> pathlib.Path:
    retval = config.inventory_dir() / f"{project}{RECORD_SUFFIX}"
    return retval


def load(project: str) -> ProjectRecord | None:
    path = record_path(project)
    retval = None
    if path.is_file():
        retval = ProjectRecord(**json.loads(path.read_text()))
    return retval


def require(project: str) -> ProjectRecord:
    retval = load(project)
    if retval is None:
        raise InventoryError(f"no project named {project} in the inventory at {config.inventory_dir()}; "
                             "`sandboxed-devenv ls` lists them")
    return retval


def save(record: ProjectRecord) -> pathlib.Path:
    retval = record_path(record.project)
    retval.parent.mkdir(parents=True, exist_ok=True)
    temporary = retval.with_name(f".{retval.name}.tmp")
    temporary.write_text(record.to_json())
    temporary.replace(retval)
    return retval


def delete(project: str) -> bool:
    path = record_path(project)
    retval = path.exists()
    if retval:
        path.unlink()
    return retval


def list_projects() -> list[ProjectRecord]:
    retlist = []
    inventory = config.inventory_dir()
    if inventory.is_dir():
        for path in sorted(inventory.glob(f"*{RECORD_SUFFIX}")):
            retlist.append(ProjectRecord(**json.loads(path.read_text())))
    return retlist


def used_addresses() -> set[str]:
    retset = set()
    for record in list_projects():
        if record.address:
            retset.add(record.address)
    return retset


def address_answers(address: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Something at the address accepts SSH connections: a VM the inventory does not know."""
    try:
        with socket.create_connection((address, 22), timeout=timeout):
            retval = True
    except OSError:
        retval = False
    return retval


def allocate_address(user_config: config.UserConfig, probe=None, report=print) -> str:
    """The lowest free address of the UTM pool: not in the inventory, not the client host's or
    the image build's, and not answering on port 22 (an older VM the inventory never saw)."""
    if probe is None:
        probe = address_answers
    utm = user_config.utm
    network = ipaddress.ip_network(utm.subnet)
    reserved = used_addresses() | {utm.client_host_address, utm.image_build_address, utm.gateway}
    candidate = ipaddress.ip_address(utm.address_pool_start)
    retval = None
    while candidate in network and candidate != network.broadcast_address:
        text = str(candidate)
        if text not in reserved:
            if probe(text):
                report(f"address {text} answers on port 22 but is not in the inventory; skipping it")
            else:
                retval = text
                break
        candidate += 1
    if retval is None:
        raise InventoryError(f"no free address in {utm.subnet} from {utm.address_pool_start} upward")
    return retval
