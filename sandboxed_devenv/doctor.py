"""`sandboxed-devenv doctor`: which roles this machine can play, and what ~/.sandboxed_devenv/ holds."""

import platform
import shutil
import subprocess
import sys

from sandboxed_devenv import common, config

DRIVER_COMMANDS = {"utm": "utmctl", "incus": "incus"}
GITHUB_CLI = "gh"
DISK_TOOLS = ("qemu-img", "hdiutil")
CLIENT_SANDBOX_USER = "gw"


def register(subparsers):
    parser = subparsers.add_parser(
        "doctor",
        help="which roles this machine can play, the drivers found, what ~/.sandboxed_devenv holds (any role)",
    )
    parser.set_defaults(handler=cmd_doctor)


def _user_exists(name: str) -> bool:
    try:
        import pwd

        pwd.getpwnam(name)
        retval = True
    except (ImportError, KeyError):
        retval = False
    return retval


def _source_text(source: str, reference: str | None) -> str:
    if source == config.SOURCE_ONEPASSWORD:
        retval = f"1Password reference {reference}"
    else:
        retval = source
    return retval


def _dir_line(label: str, path) -> str:
    state = "present" if path.is_dir() else "absent"
    retval = f"{label}: {path} ({state})"
    return retval


def cmd_doctor(args) -> int:
    found = {}
    for driver, command in DRIVER_COMMANDS.items():
        found[driver] = shutil.which(command)

    lines = []
    lines.append(
        f"sandboxed-devenv {common.package_version()} on {platform.system()} {platform.release()} "
        f"({platform.machine()}), Python {platform.python_version()} at {sys.executable}"
    )
    driver_parts = []
    for driver, path in found.items():
        driver_parts.append(f"{driver}: {path or 'not found'}")
    lines.append("drivers: " + "; ".join(driver_parts))
    if platform.system() == "Darwin":
        # The UTM driver's helpers: qemu-img converts the QCOW2 cloud image to the raw disk the Apple
        # backend takes (brew install qemu); hdiutil writes the seed ISO and ships with macOS.
        tool_parts = []
        for command in DISK_TOOLS:
            tool_parts.append(f"{command}: {shutil.which(command) or 'not found'}")
        lines.append("disk tools: " + "; ".join(tool_parts))

    gh = shutil.which(GITHUB_CLI)
    if gh:
        completed = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True)
        if completed.returncode == 0 and completed.stdout.strip():
            lines.append(f"github cli: {gh}, logged in as {completed.stdout.strip()} (deploy keys are registered through it)")
        else:
            lines.append(f"github cli: {gh}, not logged in; `gh auth login` before `new --repo` or `repo add`")
    else:
        lines.append("github cli: gh not found; `new --repo` and `repo add` need it to register deploy keys")
    usable = [driver for driver, path in found.items() if path]
    if usable:
        control = "yes (" + ", ".join(usable) + ")"
    else:
        control = "no (neither utmctl nor incus found)"
    if platform.system() != "Linux":
        client_host = "no (not Linux)"
    elif _user_exists(CLIENT_SANDBOX_USER):
        client_host = f"yes (user {CLIENT_SANDBOX_USER} exists)"
    else:
        client_host = f"not provisioned (no user {CLIENT_SANDBOX_USER}; run `sandboxed-devenv host install`)"
    if config.BACKEND_PROJECT_CONFIG.is_file():
        backend = f"yes ({config.BACKEND_PROJECT_CONFIG} present)"
    else:
        backend = f"no ({config.BACKEND_PROJECT_CONFIG} absent)"
    lines.append(f"roles: control: {control}; client host: {client_host}; backend: {backend}")

    lines.append(_dir_line("home", config.home_dir()))
    config_state = "present" if config.config_file().is_file() else "absent"
    lines.append(f"config: {config.config_file()} ({config_state})")
    inventory = config.inventory_dir()
    projects = len(list(inventory.glob("*.json"))) if inventory.is_dir() else 0
    lines.append(f"inventory: {inventory} ({'present' if inventory.is_dir() else 'absent'}, {projects} projects)")
    keys = []
    for name in (config.CONTROL_KEY_NAME, config.SANDBOX_KEY_NAME):
        keys.append(f"{name} {'present' if (config.secrets_dir() / name).is_file() else 'absent'}")
    if (config.secrets_dir() / config.LEGACY_RIG_KEY_NAME).is_file():
        keys.append(f"{config.LEGACY_RIG_KEY_NAME} present (becomes the control key on the next VM command)")
    lines.append(f"{_dir_line('secrets', config.secrets_dir())}: {'; '.join(keys)}")
    user_config = config.load_user_config()
    tokens = []
    source, reference = config.secret_source(config.CLAUDE_TOKEN_NAME)
    tokens.append(f"{config.CLAUDE_TOKEN_NAME}: {_source_text(source, reference)}")
    if config.secrets_dir().is_dir():
        for path in sorted(config.secrets_dir().glob(f"{config.CLAUDE_TOKEN_NAME}.*")):
            source, reference = config.secret_source(path.name)
            tokens.append(f"{path.name}: {_source_text(source, reference)}")
    op = shutil.which(config.OP_COMMAND)
    lines.append(f"claude token policy: {user_config.backend.claude_token}"
                 f"{', disposable backends get one' if user_config.backend.disposable_claude_token else ', none for disposable backends'}; "
                 + "; ".join(tokens) + f"; 1Password CLI: {op or 'not found'}")
    policy_source = "the user's" if config.policy_file() == config.user_policy_file() else "the package's"
    lines.append(f"policy: {config.policy_file()} ({policy_source})")
    for line in lines:
        print(line)
    retval = 0
    return retval
