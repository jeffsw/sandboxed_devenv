"""`sandboxed-devenv render`: every artifact without touching a VM; CI's entry point.

Also the package's view of ``files/`` (PLAN.md, "Files and templates"): the manifest, the naming
rule and the ``@@NAME@@`` fill, which `host install` and, from M2, the drivers use to put each
file where it goes. Rendering has no side effects beyond the output directory.

The values the templates take come from ``versions.yaml`` (every pinned version), the user
config (addresses, the VM name prefix), ``policy.yaml`` (the VM subnet, the uplink, the egress
allowlists that become nftables set elements) and, per project, ``project_values()``. The desk
VM's login password keeps the rig's environment override (``DESK_PASSWORD``).
"""

import base64
import json
import os
import pathlib
import pwd
import re
import shlex
import shutil
import subprocess
import sys

import pydantic
import yaml

from sandboxed_devenv import config, remote

CHECKOUT_MARKER = 'name = "sandboxed-devenv"'
ROLES = ("host", "backend")
CLOUDINIT_PREFIX = "cloudinit"
TEMPLATE_SUFFIX = ".tmpl"
CLOUDINIT_FORMAT_SUFFIX = ".yaml"
PLACEHOLDER = re.compile(r"@@([A-Z][A-Z0-9_]*)@@")

DEFAULT_DESK_PASSWORD = "rehearsal"
DEFAULT_CLIENT_HOST_USER = "jsw"
# The client sandbox's user on the client host, whose egress the ruleset's output chain limits.
# nft resolves a user name when the rules are parsed, so the ruleset carries the uid: the real
# one where the user exists (`host install` creates it first), else this placeholder (CI).
SANDBOX_USER = "gw"
SANDBOX_UID_PLACEHOLDER = 1001
# What `render` puts in a document when the secrets directory holds no key pair yet: CI renders
# with placeholders, and secrets never enter its artifacts (PLAN.md, "Principles").
PUBKEY_PLACEHOLDER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA placeholder"
# Every seed carries the VM's SSH host key pair (cloud-init's `ssh_keys`), minted by the control
# side so the public half is pinned before the VM boots. The image record's digest of the
# image cloud-init is taken with these fixed values instead, or every build would look new.
HOST_KEY_DIGEST_VALUES = {"HOST_KEY_PRIVATE": "    (a fresh key per boot)", "HOST_KEY_PUBLIC": "ssh-ed25519 (a fresh key per boot)"}
HOST_KEY_INDENT = "    "
SAMPLE_PROJECT = "sample"
_sample_host_key = None


class RenderError(Exception):
    """A placeholder without a value, or a manifest entry without its file."""


class ManifestEntry(pydantic.BaseModel):
    """One entry of files/manifest.yaml; the key it sits under is the source file's name."""

    model_config = pydantic.ConfigDict(extra="forbid")

    dest: str | None = None
    owner: str | None = None
    group: str | None = None
    mode: str | None = None
    template: bool
    stage: str | None = None
    # Written when absent and left alone afterwards: another command owns its content.
    keep: bool = False
    description: str

    @property
    def is_cloudinit(self) -> bool:
        """A cloud-init document has no destination; the name says which NoCloud file it is."""
        retval = self.dest is None
        return retval

    @property
    def mode_bits(self) -> int:
        retval = int(self.mode, 8)
        return retval


def register(subparsers):
    parser = subparsers.add_parser(
        "render",
        help="render every artifact and build the wheel, without touching a VM (any role; CI's entry point)",
    )
    parser.add_argument(
        "--out",
        default="build",
        help="output directory, relative to the checkout unless absolute (default: build/)",
    )
    parser.set_defaults(handler=cmd_render)


def files_root() -> pathlib.Path:
    """``files/`` inside the installed package (package data, a real directory in every install)."""
    retval = pathlib.Path(__file__).resolve().parent / "files"
    return retval


def load_manifest() -> dict[str, dict[str, ManifestEntry]]:
    """``{role: {source name: entry}}`` from files/manifest.yaml."""
    raw = yaml.safe_load((files_root() / "manifest.yaml").read_text())
    retdict = {}
    for role, entries in raw.items():
        retdict[role] = {}
        for name, fields in (entries or {}).items():
            retdict[role][name] = ManifestEntry(**fields)
    return retdict


def encoded_dest(dest: str) -> str:
    """The naming rule: leading slash dropped, other slashes as ``__``, dots kept."""
    retval = dest.lstrip("/").replace("/", "__")
    return retval


def cloudinit_file_name(source_name: str) -> tuple[str, str]:
    """``cloudinit-image__user-data.yaml.tmpl`` gives ``("cloudinit-image", "user-data")``."""
    kind, _, rest = source_name.partition("__")
    if rest.endswith(TEMPLATE_SUFFIX):
        rest = rest[: -len(TEMPLATE_SUFFIX)]
    if rest.endswith(CLOUDINIT_FORMAT_SUFFIX):
        rest = rest[: -len(CLOUDINIT_FORMAT_SUFFIX)]
    retval = (kind, rest)
    return retval


def fill(text: str, values: dict[str, str], where: str) -> str:
    """Replace every ``@@NAME@@``; a name without a value is an error, not a silent blank."""
    missing = []
    for name in sorted(set(PLACEHOLDER.findall(text))):
        if name not in values:
            missing.append(name)
    if missing:
        raise RenderError(f"{where}: no value for {', '.join(missing)}")
    retval = PLACEHOLDER.sub(lambda match: values[match.group(1)], text)
    return retval


def nft_elements(items: list) -> str:
    """A set's elements for nftables, or a comment when the list is empty (nft rejects `{ }`)."""
    if items:
        retval = "elements = { " + ", ".join(str(item) for item in items) + " }"
    else:
        retval = "# no elements"
    return retval


def nft_ports(ports: list[int]) -> str:
    retval = "{ " + ", ".join(str(port) for port in ports) + " }"
    return retval


def sandbox_uid() -> int:
    """The sandbox user's uid on this machine, or the placeholder where the user does not exist."""
    try:
        retval = pwd.getpwnam(SANDBOX_USER).pw_uid
    except KeyError:
        retval = SANDBOX_UID_PLACEHOLDER
    return retval


def client_host_user(user_config: config.UserConfig) -> str:
    """The login on the client host VM: the user in ``client_host``, or the rig's default."""
    parsed = remote.parse_client_host(user_config.client_host) if user_config.client_host != "local" else None
    retval = parsed[0] if parsed else DEFAULT_CLIENT_HOST_USER
    return retval


def default_values(user_config: config.UserConfig | None = None, policy: config.Policy | None = None,
                   **overrides: str) -> dict[str, str]:
    """Every value the templates may use that is not specific to one project: the pinned versions,
    the addresses from the user config, the policy's subnet and allowlists, and secrets
    (``CONTROL_PUBKEY``, ``SANDBOX_PUBKEY``, the host key pair) only as overrides."""
    versions = config.load_versions()
    if user_config is None:
        user_config = config.load_user_config()
    if policy is None:
        policy = config.load_policy()
    utm = user_config.utm
    lower_half, upper_half = policy.subnet_halves()
    retdict = {
        "GATEWAY_VERSION": versions.gateway.version,
        # Gateway keeps its settings under ~/.config/JetBrains/JetBrainsGateway<major.minor>/
        "GATEWAY_BRANCH": versions.gateway.branch,
        "IDE_BACKEND_VERSION": versions.ide_backend.version,
        "IDE_BACKEND_URL_AARCH64": versions.ide_backend.url("aarch64"),
        "IDE_BACKEND_URL_X86_64": versions.ide_backend.url("x86_64"),
        "IDE_PLUGINS": " ".join(versions.ide_backend.plugins),
        # The nftables ruleset, from policy.yaml.
        "VM_SUBNET": policy.vm_subnet,
        "UPLINK_INTERFACE": policy.uplink_interface,
        "SANDBOX_USER": SANDBOX_USER,
        "SANDBOX_UID": str(sandbox_uid()),
        "CLIENT_ALLOWED": nft_elements(policy.client.cidrs),
        "CLIENT_PORTS": nft_ports(policy.client.ports),
        "CLIENT_HOSTS": ", ".join(policy.client.hosts) or "none",
        "BACKEND_ALLOWED": nft_elements(policy.backend.cidrs),
        "BACKEND_PORTS": nft_ports(policy.backend.ports),
        "BACKEND_HOSTS": ", ".join(policy.backend.hosts) or "none",
        # The desk VM on the Mac rig: its login, and the two half-subnet routes through the Mac.
        "CLIENT_HOST_USER": client_host_user(user_config),
        "DESK_PASSWORD": os.environ.get("DESK_PASSWORD", DEFAULT_DESK_PASSWORD),
        "SUBNET_LOWER_HALF": lower_half,
        "SUBNET_UPPER_HALF": upper_half,
        # A backend's network-config on UTM; `new` adds ADDRESS from the pool.
        "INTERFACE": utm.interface,
        "PREFIX_LENGTH": utm.subnet.split("/")[1],
        "GATEWAY": utm.gateway,
        "NAMESERVER": utm.nameserver,
        "CLIENT_HOST_ADDRESS": utm.client_host_address,
    }
    retdict.update(overrides)
    return retdict


def project_values(project: config.ProjectConfig, hostname: str, address: str | None = None) -> dict[str, str]:
    """What one project's backend documents need: its name, project.yaml for cloud-init to write,
    Claude's env block, and on UTM its static address."""
    if project.claude_oauth_token:
        env = {"CLAUDE_CODE_OAUTH_TOKEN": project.claude_oauth_token}
    else:
        env = {}
    retdict = {
        "HOSTNAME": hostname,
        "PROJECT_YAML_B64": base64.b64encode(project.to_yaml().encode()).decode(),
        "CLAUDE_ENV_JSON": json.dumps(env),
    }
    if address is not None:
        retdict["ADDRESS"] = address
    return retdict


def host_key_values(private: str, public: str) -> dict[str, str]:
    """The seed's host key pair as the templates take it: the private half indented into the
    `ed25519_private: |` block scalar, the public half on one line."""
    lines = []
    for line in private.rstrip("\n").splitlines():
        lines.append(f"{HOST_KEY_INDENT}{line}")
    retdict = {"HOST_KEY_PRIVATE": "\n".join(lines), "HOST_KEY_PUBLIC": public.strip()}
    return retdict


def sample_host_key() -> dict[str, str]:
    """A throwaway host key pair for `render` and the tests, generated once per process."""
    global _sample_host_key
    if _sample_host_key is None:
        private, public = remote.generate_host_key("sample")
        _sample_host_key = host_key_values(private, public)
    retdict = dict(_sample_host_key)
    return retdict


def sample_project() -> config.ProjectConfig:
    """A project with no secrets, so `render` and CI can render the project stage."""
    retval = config.ProjectConfig(
        project=SAMPLE_PROJECT,
        repositories=[config.Repository(name="example/repository")],
        git=config.GitIdentity(user_name="Developer", user_email="developer@example.invalid"),
    )
    return retval


def sample_values(**overrides: str) -> dict[str, str]:
    """Defaults plus the sample project, placeholder public keys and a throwaway host key, for
    `render` and the tests; the overrides win."""
    user_config = config.load_user_config()
    retdict = default_values(user_config)
    retdict.update(project_values(sample_project(), user_config.vm_name(SAMPLE_PROJECT), user_config.utm.address_pool_start))
    retdict["CONTROL_PUBKEY"] = PUBKEY_PLACEHOLDER
    retdict["SANDBOX_PUBKEY"] = PUBKEY_PLACEHOLDER
    retdict.update(sample_host_key())
    retdict.update(overrides)
    return retdict


def secret_public_key(name: str) -> str | None:
    """A key pair's public half from the secrets directory, or None when there is none yet."""
    path = config.secrets_dir() / f"{name}.pub"
    retval = None
    if path.is_file():
        retval = path.read_text().strip()
    return retval


def source_path(role: str, name: str) -> pathlib.Path:
    retval = files_root() / role / name
    return retval


def render_entry(role: str, name: str, entry: ManifestEntry, values: dict[str, str]) -> str:
    """The file's content as the role's machine receives it."""
    text = source_path(role, name).read_text()
    if entry.template:
        retval = fill(text, values, f"files/{role}/{name}")
    else:
        retval = text
    return retval


def rendered_dest(entry: ManifestEntry, values: dict[str, str]) -> pathlib.Path:
    """The destination with its own placeholders filled, such as the Gateway branch."""
    retval = pathlib.Path(fill(entry.dest, values, f"dest {entry.dest}"))
    return retval


def render_role(role: str, values: dict[str, str], out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Write one role's files under ``out_dir/<role>/files/<dest>`` and its cloud-init documents under
    ``out_dir/<role>/<cloudinit kind>/<NoCloud name>``; returns what was written."""
    retlist = []
    for name, entry in load_manifest()[role].items():
        content = render_entry(role, name, entry, values)
        if entry.is_cloudinit:
            kind, file_name = cloudinit_file_name(name)
            target = out_dir / role / kind / file_name
        else:
            target = out_dir / role / "files" / rendered_dest(entry, values).relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        retlist.append(target)
    return retlist


def cloudinit_document(role: str, kind: str, file_name: str, values: dict[str, str]) -> str:
    """One rendered cloud-init document, found by its kind (``cloudinit-image``, ``cloudinit``) and
    NoCloud file name (``user-data``, ``network-config``)."""
    retval = None
    for name, entry in load_manifest()[role].items():
        if entry.is_cloudinit and cloudinit_file_name(name) == (kind, file_name):
            retval = render_entry(role, name, entry, values)
            break
    if retval is None:
        raise RenderError(f"no {kind} {file_name} document for role {role} in the manifest")
    return retval


def meta_data(instance_id: str, hostname: str) -> str:
    """NoCloud's meta-data: a new instance id makes cloud-init treat the boot as a first boot."""
    retval = f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"
    return retval


def write_seed(seed_dir: pathlib.Path, user_data: str, instance_id: str, hostname: str,
               network_config: str | None = None) -> list[pathlib.Path]:
    """A NoCloud seed directory: user-data, meta-data and, for a static address, network-config.
    The driver turns it into an ISO (UTM) or instance config keys (Incus)."""
    seed_dir.mkdir(parents=True, exist_ok=True)
    retlist = []
    documents = {"user-data": user_data, "meta-data": meta_data(instance_id, hostname)}
    if network_config is not None:
        documents["network-config"] = network_config
    for name, text in documents.items():
        path = seed_dir / name
        path.write_text(text)
        retlist.append(path)
    return retlist


def build_wheel(checkout: pathlib.Path, out_dir: pathlib.Path, runner=None) -> pathlib.Path:
    """The wheel built from the checkout into ``out_dir``; the newest one there afterwards."""
    out_dir.mkdir(parents=True, exist_ok=True)
    command = wheel_build_command(out_dir)
    if runner is None:
        completed = subprocess.run(command, cwd=checkout, text=True, capture_output=True)
    else:
        completed = runner.run(command, cwd=checkout, capture_output=True)
    if completed.returncode != 0:
        raise RenderError(f"{shlex.join(command)} failed with status {completed.returncode}: "
                          f"{(completed.stderr or '').strip()[-400:]}")
    wheels = sorted(out_dir.glob("sandboxed_devenv-*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels and not (runner and runner.dry_run):
        raise RenderError(f"no wheel under {out_dir} after {shlex.join(command)}")
    retval = wheels[-1] if wheels else out_dir / "sandboxed_devenv-unbuilt.whl"
    return retval


def find_checkout(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """The repository root above `start` (default: the working directory), or None outside a checkout."""
    retval = None
    here = (start or pathlib.Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and CHECKOUT_MARKER in pyproject.read_text():
            retval = candidate
            break
    return retval


def wheel_build_command(out_dir: pathlib.Path) -> list[str]:
    """uv when present (pinned by mise), else the `build` package from the dev group."""
    if shutil.which("uv"):
        retval = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        retval = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    return retval


def cmd_render(args) -> int:
    checkout = find_checkout()
    if checkout is None:
        print("sandboxed-devenv render: run it inside a checkout of the repository (no pyproject.toml found)",
              file=sys.stderr)
        retval = 1
        return retval
    out_dir = pathlib.Path(args.out)
    if not out_dir.is_absolute():
        out_dir = checkout / out_dir
    overrides = {}
    missing = []
    for placeholder, name in (("CONTROL_PUBKEY", config.CONTROL_KEY_NAME), ("SANDBOX_PUBKEY", config.SANDBOX_KEY_NAME)):
        pubkey = secret_public_key(name)
        if pubkey:
            overrides[placeholder] = pubkey
        else:
            missing.append(name)
    command = wheel_build_command(out_dir)
    if args.dry_run:
        key_state = "present" if not missing else f"{', '.join(missing)} absent, so placeholder keys are rendered"
        print(f"would render files/host and files/backend under {out_dir} (key pairs in {config.secrets_dir()}: {key_state})")
        print(f"would run in {checkout}: {shlex.join(command)}")
        retval = 0
        return retval
    if missing:
        print(f"render: no {' or '.join(missing)} in {config.secrets_dir()}; rendering a placeholder key (`image build`, "
              "`client-host new` or `new` mints the real ones)")
    values = sample_values(**overrides)
    for role in ROLES:
        try:
            written = render_role(role, values, out_dir)
        except RenderError as error:
            print(f"sandboxed-devenv render: {error}", file=sys.stderr)
            retval = 1
            return retval
        print(f"render: {len(written)} {role} files under {out_dir / role}")
    out_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, cwd=checkout)
    if completed.returncode != 0:
        retval = 1
        return retval
    wheels = sorted(out_dir.glob("sandboxed_devenv-*.whl"))
    if wheels:
        print(f"render: wheel {wheels[-1]}")
    retval = 0
    return retval
