"""`sandboxed-devenv connect ...`: the client host's side of a backend connection (client host role).

Three things make a backend reachable from the client sandbox, and `connect add` writes all of
them as root on the client host, where `new` runs it through remote.py:

- a stanza in ``/home/gw/.ssh/config.d/<project>``: ``Host <project> <address>``, user dev,
  gw's key, the pinned known_hosts file and strict host key checking, so ``ssh <project>``
  works for gw and Gateway's OpenSSH-config lookup finds the same settings;
- the backend's host keys in ``/home/gw/.ssh/known_hosts``, pinned by address, so neither gw
  nor Gateway meets a trust-on-first-use prompt (M2 takes the keys from `backend status` after
  the control side's own first connection; M3 moves the pinning before any connection);
- an entry in Gateway's ``sshConfigs.xml``. Gateway reloads the file when its window regains
  focus and keeps what it read when it exits (tried on desk, 8 November 2026: an entry written
  under a running Gateway appeared in its dialog after a click on the window, opened a session,
  and was still in the file after Gateway quit), so the entry is written whether or not Gateway
  runs. With Gateway running the command says so in bold yellow, since the user has something
  to do: a window that already has focus must lose and regain it for the reload.

`connect rm` undoes the three; `connect ls` lists what is there. Nothing here touches routes:
the desk cloud-init carries routes to both halves of the VM subnet (PLAN.md M2), and the Linux
laptop is on the VMs' bridge itself.
"""

import dataclasses
import json
import os
import pathlib
import platform
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ElementTree

from sandboxed_devenv import common, host_client, render, steps
from sandboxed_devenv.steps import UNCHANGED, Outcome, read_text, remove_file, sync_file

COMMAND = "connect"
STANZA_HEADER = "# sandboxed-devenv connect: project "
BACKEND_USER = "dev"
SSH_PORT = "22"
# Gateway's macro for the user's home, as it writes key paths itself.
KEY_PATH_MACRO = "$USER_HOME$/.ssh/vm_ed25519"
# Entry ids are derived from the project name, so re-adding a project changes nothing.
ENTRY_NAMESPACE = uuid.UUID("5a3a4c6e-2d4b-4f5e-9c1a-7b8d9e0f1a2b")
SSH_CONFIGS_HEADER = ("<!-- sandboxed-devenv host: /home/gw/.config/JetBrains/JetBrainsGateway{branch}/options/sshConfigs.xml, "
                      "managed by `sandboxed-devenv connect` -->\n")


@dataclasses.dataclass
class Paths:
    """Where the client sandbox's files live; the defaults are gw's, tests use a temporary home."""

    home: pathlib.Path = host_client.GW_HOME
    user: str = host_client.GW_USER
    group: str = host_client.GW_USER
    gateway_branch: str = ""

    @property
    def ssh_dir(self) -> pathlib.Path:
        retval = self.home / ".ssh"
        return retval

    @property
    def config_dir(self) -> pathlib.Path:
        retval = self.ssh_dir / "config.d"
        return retval

    @property
    def known_hosts(self) -> pathlib.Path:
        retval = self.ssh_dir / "known_hosts"
        return retval

    @property
    def key_file(self) -> pathlib.Path:
        retval = self.ssh_dir / "vm_ed25519"
        return retval

    @property
    def gateway_dir(self) -> pathlib.Path:
        retval = self.home / "gateway"
        return retval

    @property
    def ssh_configs(self) -> pathlib.Path:
        retval = (self.home / ".config" / "JetBrains" / f"JetBrainsGateway{self.gateway_branch}" / "options"
                  / "sshConfigs.xml")
        return retval

    def stanza_file(self, project: str) -> pathlib.Path:
        retval = self.config_dir / project
        return retval


def default_paths() -> Paths:
    retval = Paths(gateway_branch=render.default_values()["GATEWAY_BRANCH"])
    return retval


def register(subparsers):
    connect = common.add_group(
        subparsers, "connect", "Gateway's sshConfigs.xml entry, gw's ssh_config stanza, the pinned host key (client host)"
    )

    add = connect.add_parser("add", help="write the stanza, the pinned keys and the Gateway entry for a project's backend; needs root")
    add.add_argument("project", metavar="PROJECT")
    add.add_argument("address", metavar="ADDRESS", help="the backend's address")
    add.add_argument("hostkeys", metavar="HOSTKEY", nargs="+",
                     help="the backend's SSH host public keys (`type base64`, quoted), pinned in known_hosts")
    add.set_defaults(handler=cmd_add)

    rm = connect.add_parser("rm", help="remove a project's stanza, pinned keys and Gateway entry; needs root")
    rm.add_argument("project", metavar="PROJECT")
    rm.set_defaults(handler=cmd_rm)

    ls = connect.add_parser("ls", help="the connections this client host knows")
    ls.add_argument("--json", action="store_true", help="machine-readable")
    ls.set_defaults(handler=cmd_ls)


# --- the three files -------------------------------------------------------------------------


def stanza_text(project: str, address: str, paths: Paths) -> str:
    retval = (
        f"{STANZA_HEADER}{project}\n"
        f"Host {project} {address}\n"
        f"    HostName {address}\n"
        f"    User {BACKEND_USER}\n"
        f"    Port {SSH_PORT}\n"
        f"    IdentityFile {paths.key_file}\n"
        f"    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {paths.known_hosts}\n"
        f"    StrictHostKeyChecking yes\n"
    )
    return retval


def parse_stanza(text: str) -> tuple[str, str] | None:
    """(project, address) from a stanza this module wrote, else None."""
    project = None
    address = None
    for line in text.splitlines():
        if line.startswith(STANZA_HEADER):
            project = line[len(STANZA_HEADER):].strip()
        elif line.strip().startswith("HostName "):
            address = line.split(None, 1)[1].strip()
    retval = (project, address) if project and address else None
    return retval


def normalize_keys(given: list[str]) -> list[str]:
    """``type base64`` per key, whatever quoting or comments the caller used."""
    retlist = []
    for item in given:
        for line in item.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                retlist.append(f"{parts[0]} {parts[1]}")
    return retlist


def known_hosts_names(line: str) -> list[str]:
    """The host names field of a known_hosts line (comma separated, maybe ``[host]:port``)."""
    retlist = []
    stripped = line.strip()
    if stripped and not stripped.startswith(("#", "|", "@")):
        for name in stripped.split()[0].split(","):
            retlist.append(name.strip("[]").split("]:")[0])
    return retlist


def without_pinned_keys(existing: str, address: str) -> str:
    kept = []
    for line in existing.splitlines():
        if address not in known_hosts_names(line):
            kept.append(line)
    retval = "\n".join(kept)
    if retval:
        retval += "\n"
    return retval


def with_pinned_keys(existing: str, address: str, keys: list[str]) -> str:
    retval = without_pinned_keys(existing, address)
    for key in keys:
        retval += f"{address} {key}\n"
    return retval


def entry_id(project: str) -> str:
    retval = str(uuid.uuid5(ENTRY_NAMESPACE, project))
    return retval


def parse_ssh_configs(text: str | None) -> ElementTree.Element:
    """Gateway's document, with its ``SshConfigs`` component and ``configs`` element present;
    created from nothing when the file is absent or empty."""
    root = None
    if text and text.strip():
        root = ElementTree.fromstring(text)
    if root is None:
        root = ElementTree.Element("application")
    component = None
    for candidate in root.findall("component"):
        if candidate.get("name") == "SshConfigs":
            component = candidate
            break
    if component is None:
        component = ElementTree.SubElement(root, "component", name="SshConfigs")
    if component.find("configs") is None:
        ElementTree.SubElement(component, "configs")
    retval = root
    return retval


def configs_element(root: ElementTree.Element) -> ElementTree.Element:
    retval = None
    for component in root.findall("component"):
        if component.get("name") == "SshConfigs":
            retval = component.find("configs")
            break
    if retval is None:
        raise ValueError("no SshConfigs component")
    return retval


def serialize_ssh_configs(root: ElementTree.Element, branch: str) -> str:
    ElementTree.indent(root, space="  ")
    retval = SSH_CONFIGS_HEADER.format(branch=branch) + ElementTree.tostring(root, encoding="unicode") + "\n"
    return retval


def ssh_configs_with(text: str | None, project: str, address: str, branch: str) -> str:
    root = parse_ssh_configs(text)
    configs = configs_element(root)
    wanted_id = entry_id(project)
    for entry in list(configs.findall("sshConfig")):
        if entry.get("id") == wanted_id or entry.get("host") == address:
            configs.remove(entry)
    ElementTree.SubElement(configs, "sshConfig", host=address, id=wanted_id, keyPath=KEY_PATH_MACRO, port=SSH_PORT,
                           nameFormat="DESCRIPTIVE", username=BACKEND_USER, useOpenSSHConfig="true")
    retval = serialize_ssh_configs(root, branch)
    return retval


def ssh_configs_without(text: str | None, project: str, branch: str) -> str:
    root = parse_ssh_configs(text)
    configs = configs_element(root)
    wanted_id = entry_id(project)
    for entry in list(configs.findall("sshConfig")):
        if entry.get("id") == wanted_id:
            configs.remove(entry)
    retval = serialize_ssh_configs(root, branch)
    return retval


def ssh_configs_entries(text: str | None) -> list[dict]:
    retlist = []
    if text and text.strip():
        for entry in configs_element(parse_ssh_configs(text)).findall("sshConfig"):
            retlist.append({"host": entry.get("host"), "id": entry.get("id"), "username": entry.get("username")})
    return retlist


def gateway_running(paths: Paths) -> bool:
    """A Gateway process of the sandbox user: anything running out of its gateway directory."""
    completed = subprocess.run(["pgrep", "-u", paths.user, "-f", f"{paths.gateway_dir}/"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    retval = completed.returncode == 0
    return retval


# --- commands --------------------------------------------------------------------------------


def can_run(command: str, dry_run: bool) -> bool:
    """Linux and root, or a dry run anywhere."""
    retval = True
    if dry_run:
        return retval
    if not common.linux_only(command):
        retval = False
    elif os.geteuid() != 0:
        print(f"sandboxed-devenv: `{command}` needs root: sudo sandboxed-devenv {command}", file=sys.stderr)
        retval = False
    return retval


def inspectable(dry_run: bool) -> bool:
    """A dry run off a client host prints the plan instead of inspecting files it cannot see."""
    retval = not dry_run or (platform.system() == "Linux" and os.geteuid() == 0)
    return retval


def finish(command: str, outcomes: list[Outcome], dry_run: bool) -> int:
    for result in outcomes:
        print(result.line(command), flush=True)
    counts = steps.tally(outcomes)
    print(steps.summary_line(command, "dry run" if dry_run else "apply", counts))
    retval = steps.exit_status(counts, check=False)
    return retval


def cmd_add(args) -> int:
    command = f"{COMMAND} add"
    if not can_run(command, args.dry_run):
        retval = 1
        return retval
    paths = default_paths()
    keys = normalize_keys(args.hostkeys)
    if not keys:
        print(f"sandboxed-devenv: `{command}` needs at least one host key as `type base64`", file=sys.stderr)
        retval = 2
        return retval
    if not inspectable(args.dry_run):
        print(f"{command}: the plan for {args.project} at {args.address} (a dry run off a client host):")
        print(f"  file {paths.stanza_file(args.project)}: Host {args.project} {args.address}, user {BACKEND_USER}, "
              f"key {paths.key_file}, strict host key checking")
        print(f"  file {paths.known_hosts}: {len(keys)} key(s) pinned for {args.address}")
        print(f"  file {paths.ssh_configs}: an sshConfig entry {entry_id(args.project)} for {args.address}, "
              "unless Gateway is running")
        retval = 0
        return retval
    apply = not args.dry_run
    outcomes = [
        sync_file(paths.stanza_file(args.project), stanza_text(args.project, args.address, paths).encode(),
                  paths.user, paths.group, 0o600, apply),
        sync_file(paths.known_hosts, with_pinned_keys(read_text(paths.known_hosts) or "", args.address, keys).encode(),
                  paths.user, paths.group, 0o644, apply),
    ]
    if gateway_running(paths):
        print(common.warning(
            f"{command}: Gateway is running as {paths.user}; it reloads {paths.ssh_configs.name} when its window regains "
            "focus: click another window, then Gateway's, and the connection is in its dialog. No restart needed."), flush=True)
    content = ssh_configs_with(read_text(paths.ssh_configs), args.project, args.address, paths.gateway_branch)
    outcomes.append(sync_file(paths.ssh_configs, content.encode(), paths.user, paths.group, 0o600, apply))
    retval = finish(command, outcomes, args.dry_run)
    return retval


def cmd_rm(args) -> int:
    command = f"{COMMAND} rm"
    if not can_run(command, args.dry_run):
        retval = 1
        return retval
    paths = default_paths()
    if not inspectable(args.dry_run):
        print(f"{command}: would remove {paths.stanza_file(args.project)}, its pinned keys from {paths.known_hosts} "
              f"and entry {entry_id(args.project)} from {paths.ssh_configs}")
        retval = 0
        return retval
    apply = not args.dry_run
    parsed = parse_stanza(read_text(paths.stanza_file(args.project)) or "")
    outcomes = [remove_file(paths.stanza_file(args.project), apply)]
    if parsed:
        known = read_text(paths.known_hosts) or ""
        stripped = without_pinned_keys(known, parsed[1])
        if stripped != known:
            outcomes.append(sync_file(paths.known_hosts, stripped.encode(), paths.user, paths.group, 0o644, apply))
        else:
            outcomes.append(Outcome(f"file {paths.known_hosts}", UNCHANGED, f"nothing pinned for {parsed[1]}"))
    text = read_text(paths.ssh_configs)
    if text and entry_id(args.project) in text:
        if gateway_running(paths):
            print(common.warning(
                f"{command}: Gateway is running as {paths.user}; click another window, then Gateway's, and the connection "
                "is gone from its dialog. No restart needed."), flush=True)
        content = ssh_configs_without(text, args.project, paths.gateway_branch)
        outcomes.append(sync_file(paths.ssh_configs, content.encode(), paths.user, paths.group, 0o600, apply))
    else:
        outcomes.append(Outcome(f"file {paths.ssh_configs}", UNCHANGED, "no entry"))
    retval = finish(command, outcomes, args.dry_run)
    return retval


def connections(paths: Paths) -> list[dict]:
    retlist = []
    pinned = set()
    for line in (read_text(paths.known_hosts) or "").splitlines():
        for name in known_hosts_names(line):
            pinned.add(name)
    entries = set()
    try:
        for entry in ssh_configs_entries(read_text(paths.ssh_configs)):
            entries.add(entry["id"])
    except ElementTree.ParseError:
        pass
    if paths.config_dir.is_dir():
        for path in sorted(paths.config_dir.iterdir()):
            parsed = parse_stanza(read_text(path) or "")
            if parsed is None:
                continue
            project, address = parsed
            retlist.append({
                "project": project,
                "address": address,
                "pinned": address in pinned,
                "gateway_entry": entry_id(project) in entries,
            })
    return retlist


def cmd_ls(args) -> int:
    paths = default_paths()
    if paths.config_dir.is_dir() and not os.access(paths.config_dir, os.R_OK):
        print(f"sandboxed-devenv: `{COMMAND} ls` reads {paths.config_dir}, which needs root here", file=sys.stderr)
        retval = 1
        return retval
    found = connections(paths)
    if args.json:
        print(json.dumps(found, indent=2))
    elif not found:
        print(f"no connections under {paths.config_dir}; `sandboxed-devenv new` adds one per project")
    else:
        for item in found:
            pinned = "pinned" if item["pinned"] else "no pinned key"
            entry = "Gateway entry" if item["gateway_entry"] else "no Gateway entry"
            print(f"{item['project']}: {item['address']} ({pinned}, {entry})")
    retval = 0
    return retval


def add_arguments(project: str, address: str, keys: list[str]) -> list[str]:
    """What the control side runs on the client host: `connect add PROJECT ADDRESS KEY...`."""
    retlist = ["connect", "add", project, address, *keys]
    return retlist


def rm_arguments(project: str) -> list[str]:
    retlist = ["connect", "rm", project]
    return retlist
