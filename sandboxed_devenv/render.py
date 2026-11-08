"""`sandboxed-devenv render`: every artifact without touching a VM; CI's entry point.

Also the package's view of ``files/`` (PLAN.md, "Files and templates"): the manifest, the naming
rule and the ``@@NAME@@`` fill, which `host install` and, from M2, the drivers use to put each
file where it goes. Rendering has no side effects beyond the output directory.

The values the templates take come from ``versions.yaml`` (every pinned version), the user
config (addresses, the VM name prefix) and, per project, ``project_values()``. Until
``policy.yaml`` arrives, the backend address, the uplink interface and the desk password keep
the rig's environment overrides (``BACKEND_ADDRESS``, ``UPLINK_INTERFACE``, ``DESK_PASSWORD``).
"""

import base64
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pydantic
import yaml

from sandboxed_devenv import config

CHECKOUT_MARKER = 'name = "sandboxed-devenv"'
ROLES = ("host", "backend")
CLOUDINIT_PREFIX = "cloudinit"
TEMPLATE_SUFFIX = ".tmpl"
CLOUDINIT_FORMAT_SUFFIX = ".yaml"
PLACEHOLDER = re.compile(r"@@([A-Z][A-Z0-9_]*)@@")

# UTM's Apple-backend NIC on desk; the Linux laptop names its own.
DEFAULT_UPLINK_INTERFACE = "en*"
DEFAULT_DESK_PASSWORD = "rehearsal"
SAMPLE_PROJECT = "sample"


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


def default_values(user_config: config.UserConfig | None = None, **overrides: str) -> dict[str, str]:
    """Every value the templates may use that is not specific to one project: the pinned versions,
    the addresses from the user config, and secrets (``RIG_PUBKEY``) only as overrides."""
    versions = config.load_versions()
    if user_config is None:
        user_config = config.load_user_config()
    utm = user_config.utm
    retdict = {
        "GATEWAY_VERSION": versions.gateway.version,
        # Gateway keeps its settings under ~/.config/JetBrains/JetBrainsGateway<major.minor>/
        "GATEWAY_BRANCH": versions.gateway.branch,
        "IDE_BACKEND_VERSION": versions.ide_backend.version,
        "IDE_BACKEND_URL_AARCH64": versions.ide_backend.url("aarch64"),
        "IDE_BACKEND_URL_X86_64": versions.ide_backend.url("x86_64"),
        "IDE_PLUGINS": " ".join(versions.ide_backend.plugins),
        # The client host's view of the backends; M2's policy.yaml widens this to the subnet.
        "BACKEND_ADDRESS": os.environ.get("BACKEND_ADDRESS", utm.address_pool_start),
        "UPLINK_INTERFACE": os.environ.get("UPLINK_INTERFACE", DEFAULT_UPLINK_INTERFACE),
        "DESK_PASSWORD": os.environ.get("DESK_PASSWORD", DEFAULT_DESK_PASSWORD),
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


def sample_project() -> config.ProjectConfig:
    """A project with no secrets, so `render` and CI can render the project stage."""
    retval = config.ProjectConfig(
        project=SAMPLE_PROJECT,
        repositories=[config.Repository(name="example/repository")],
        git=config.GitIdentity(user_name="Developer", user_email="developer@example.invalid"),
    )
    return retval


def sample_values(**overrides: str) -> dict[str, str]:
    """Defaults plus the sample project, for `render` and the tests."""
    user_config = config.load_user_config()
    retdict = default_values(user_config, **overrides)
    retdict.update(project_values(sample_project(), user_config.vm_name(SAMPLE_PROJECT), user_config.utm.address_pool_start))
    return retdict


def rig_public_key() -> str | None:
    """The rig key's public half from the secrets directory, or None when there is none yet."""
    path = config.secrets_dir() / f"{config.RIG_KEY_NAME}.pub"
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
    key_file = config.secrets_dir() / f"{config.RIG_KEY_NAME}.pub"
    pubkey = rig_public_key()
    command = wheel_build_command(out_dir)
    if args.dry_run:
        key_state = "present" if pubkey else "absent, so the real run would stop"
        print(f"would render files/host and files/backend under {out_dir} (rig key {key_file}: {key_state})")
        print(f"would run in {checkout}: {shlex.join(command)}")
        retval = 0
        return retval
    if pubkey is None:
        print(f"sandboxed-devenv render: no rig key at {key_file}; rehearsal/utm/make-seed.sh creates one",
              file=sys.stderr)
        retval = 1
        return retval
    values = sample_values(RIG_PUBKEY=pubkey)
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
