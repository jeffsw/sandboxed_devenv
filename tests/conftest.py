"""Shared fixtures. Tests import the package through importlib mode (pyproject.toml).

The ``rig`` fixture is a control machine in a temporary state directory, with the driver, the
backends, the client host and GitHub replaced by fakes that record what was asked of them.
"""

import base64
import json
import pathlib
import subprocess

import pytest
import yaml

from sandboxed_devenv import (
    canary,
    common,
    config,
    control_creds,
    control_driver,
    control_images,
    control_inventory,
    remote,
    render,
)

CHECKOUT = pathlib.Path(__file__).resolve().parent.parent
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test"
SANDBOX_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB gw@test"
# One real host key pair for every test, generated once: what the control side mints per VM.
HOST_KEY = remote.generate_host_key("test")
HOST_KEYS = [HOST_KEY[1]]


@pytest.fixture
def checkout() -> pathlib.Path:
    """The repository root, for tests that read pyproject.toml or PLAN.md."""
    retval = CHECKOUT
    return retval


class FakeDriver:
    name = "utm"
    static_network = True
    raw_disk = True
    live_resources = False

    def __init__(self, runner=None):
        self.runner = runner or common.Runner()
        self.vms = {}
        self.specs = {}
        self.seeds = {}
        self.calls = []
        self.resources_of = {}
        self.pinned_at_start = {}
        self.images = {}

    def available(self):
        retval = None
        return retval

    def address_pool(self, user_config):
        retval = control_inventory.utm_pool(user_config)
        return retval

    def image_exists(self, name):
        retval = name in self.images or control_driver.image_file(name).is_file()
        return retval

    def image_location(self, name):
        retval = str(control_driver.image_file(name))
        return retval

    def image_size(self, name):
        path = control_driver.image_file(name)
        retval = (path.stat().st_size, path.stat().st_size) if path.is_file() else (4096, 4096)
        return retval

    def keep_image(self, vm, name):
        self.calls.append(("keep_image", vm, name))
        self.images[name] = vm
        control_driver.image_file(name).parent.mkdir(parents=True, exist_ok=True)
        control_driver.image_file(name).write_bytes(b"kept")
        retval = self.image_location(name)
        return retval

    def remove_image(self, name):
        retval = self.image_exists(name)
        self.images.pop(name, None)
        if control_driver.image_file(name).is_file():
            control_driver.image_file(name).unlink()
        return retval

    def clone_from(self, source, spec):
        self.calls.append(("export", source))
        self.create(spec)

    def exists(self, name):
        retval = name in self.vms
        return retval

    def create(self, spec):
        self.calls.append(("create", spec.name))
        self.specs[spec.name] = spec
        self.seeds[spec.name] = {path.name: path.read_text() for path in spec.seed_dir.iterdir()}
        self.vms[spec.name] = control_driver.STOPPED
        self.resources_of[spec.name] = (spec.cpus, spec.memory_gib)

    def start(self, name):
        self.calls.append(("start", name))
        # Pinned before the first boot, or not at all: what the known_hosts file held at start.
        pinned = remote.known_hosts_file(name)
        self.pinned_at_start[name] = pinned.read_text() if pinned.is_file() else None
        self.vms[name] = control_driver.STARTED

    def stop(self, name, force=False):
        self.calls.append(("stop", name, force))
        self.vms[name] = control_driver.STOPPED

    def delete(self, name):
        self.calls.append(("delete", name))
        del self.vms[name]

    def clone(self, name, new_name):
        self.vms[new_name] = control_driver.STOPPED

    def list(self):
        retlist = []
        for name, state in self.vms.items():
            retlist.append(control_driver.VmInfo(name=name, state=state))
        return retlist

    def state(self, name):
        retval = self.vms[name]
        return retval

    def address(self, name):
        retval = None
        return retval

    def console_command(self, name):
        retlist = ["screen", "/dev/ttys009"]
        return retlist

    def resources(self, name):
        retval = self.resources_of.get(name)
        return retval

    def set_resources(self, name, cpus, memory_gib):
        self.calls.append(("set_resources", name, cpus, memory_gib))
        current = self.resources_of[name]
        self.resources_of[name] = (cpus or current[0], memory_gib or current[1])


def repositories_from_seed(seed: dict) -> list[str]:
    """The repositories named in a project seed's project.yaml, as the backend would mint keys for."""
    retlist = []
    document = yaml.safe_load(seed.get("user-data", "")) or {}
    for entry in document.get("write_files") or []:
        if entry.get("path") == str(config.BACKEND_PROJECT_CONFIG):
            project = yaml.safe_load(base64.b64decode(entry["content"]))
            for repository in project.get("repositories") or []:
                retlist.append(repository["name"])
    return retlist


class FakeBackend:
    """What remote.backend() returns: a ready backend with a key pair per repository it knows,
    `backend repo` commands that behave like the real ones, and a project.yaml for rotation."""

    def __init__(self, address, vm_name, runner=None, repositories=None):
        self.address = address
        self.vm_name = vm_name
        self.runner = runner or common.Runner()
        self.target = f"dev@{address}"
        self.calls = []
        self.repositories = list(repositories or [])
        self.generation = {}
        self.checkouts = set()
        self.project_yaml = "project: p\ngit:\n  user_name: D\n  user_email: d@example.invalid\n"
        self.inputs = []
        self.command_path = "/usr/local/bin/sandboxed-devenv"
        self.canary_fail = False
        self.version = "sandboxed-devenv 0.1.0"

    def key_record(self, name):
        generation = self.generation.get(name, 0)
        suffix = f"-g{generation}" if generation else ""
        retdict = {
            "repository": name, "alias": f"github.com-{name.replace('/', '-')}", "key_file": f"/home/dev/.ssh/deploy_{name.replace('/', '_')}",
            "fingerprint": f"SHA256:{name}{suffix}", "public_key": f"ssh-ed25519 KEY{name.replace('/', '-')}{suffix} comment",
            "created": False, "checkout": f"/home/dev/proj/p/{name.split('/')[1]}" if name in self.checkouts else None,
        }
        return retdict

    def wait_for_ssh(self, timeout, interval=5.0):
        self.calls.append("wait_for_ssh")
        retval = 1.0
        return retval

    def status(self):
        retdict = {
            "hostname": self.vm_name, "ready": True, "package_version": "0.1.0", "ssh_host_keys": list(HOST_KEYS),
            "cloud_init": {"status": "done", "detail": ""},
            "ide_backend": {"installed": {"name": "IntelliJ IDEA", "version": "2026.2.3", "build": "262.1"}, "registered": True},
            "repositories": [self.key_record(name) for name in self.repositories],
        }
        return retdict

    def wait_ready(self, timeout, interval=10.0):
        self.calls.append("wait_ready")
        retval = self.status()
        return retval

    def run(self, remote_command, check=True, capture=False, timeout=None, quiet=False, input=None):
        self.calls.append(("run", tuple(remote_command)))
        stdout = ""
        status = 0
        if remote_command[:2] == ["sudo", "cat"]:
            stdout = self.project_yaml
        if remote_command[:2] == ["sudo", "tee"]:
            self.project_yaml = input
            self.inputs.append(input)
        if "canary" in remote_command:
            stdout = canary_json_lines("backend", self.canary_fail)
            status = 1 if self.canary_fail else 0
        if remote_command[-1] == "--version":
            stdout = self.version + "\n"
        retval = subprocess.CompletedProcess(remote_command, status, stdout, "")
        return retval

    def tool(self, args, sudo=False, parse_json=False, check=True, json_flag=True):
        self.calls.append(("tool", tuple(args)))
        retval = {}
        if args[:2] == ["backend", "repo"]:
            assert json_flag is False, "the backend repo commands take no --json flag"
            name = args[3]
            if args[2] == "add":
                created = name not in self.repositories
                if created:
                    self.repositories.append(name)
                if "--no-clone" not in args:
                    self.checkouts.add(name)
                retval = dict(self.key_record(name), created=created)
            elif args[2] == "rekey":
                self.generation[name] = self.generation.get(name, 0) + 1
                if name not in self.repositories:
                    self.repositories.append(name)
                retval = dict(self.key_record(name), created=True)
            elif args[2] == "rm":
                removed = []
                if name in self.repositories:
                    self.repositories.remove(name)
                    removed = ["key", "key.pub", "stanza"]
                self.checkouts.discard(name)
                retval = {"repository": name, "removed": removed, "checkout": None}
        return retval

    def ssh_command(self, remote_command=None, tty=False):
        retlist = ["ssh", self.target]
        if remote_command:
            retlist.append(" ".join(remote_command))
        return retlist


def canary_json_lines(role: str, fail: bool = False) -> str:
    """What the canary prints with `--json --expect` on a healthy machine of the role, or with
    one failing probe."""
    lines = [json.dumps({"canary": {"role": role, "user": "gw" if role == "client" else "dev", "host_home": None}})]
    expected = canary.expected_for(role)
    for pattern, allowed in expected.items():
        name = pattern[:-1] + "x" if pattern.endswith("*") else pattern
        lines.append(json.dumps({"status": allowed[0], "name": name, "detail": "as expected"}))
    mismatches = []
    counts = {"pass": sum(1 for v in expected.values() if v[0] == "PASS"), "fail": 0,
              "info": sum(1 for v in expected.values() if v[0] == "INFO")}
    if fail:
        lines.append(json.dumps({"status": "FAIL", "name": "wayland", "detail": "no socket"}))
        mismatches.append("wayland: FAIL, expected PASS (no socket)")
        counts["fail"] = 1
    lines.append(json.dumps({"expect": {"source": "packaged", "mismatches": mismatches}}))
    lines.append(json.dumps({"summary": counts}))
    retval = "\n".join(lines) + "\n"
    return retval


class FakeClient:
    def __init__(self):
        self.calls = []
        self.target = "jsw@192.168.64.10"
        self.host = "192.168.64.10"
        self.command_path = "/usr/local/bin/sandboxed-devenv"
        self.fail = False
        self.drift = False
        self.canary_fail = False
        self.ssh_fail = False
        self.version = "sandboxed-devenv 0.1.0"

    def tool(self, args, sudo=False, parse_json=False, check=True, json_flag=True):
        self.calls.append((tuple(args), sudo))
        if self.fail:
            raise remote.RemoteError("boom")
        retval = subprocess.CompletedProcess(args, 0, "", "")
        return retval

    def run(self, command, check=True, capture=False, timeout=None, quiet=False, input=None):
        self.calls.append(("run", tuple(command)))
        stdout = ""
        stderr = ""
        status = 0
        if "--check" in command:
            status = 1 if self.drift else 0
            stdout = "host install: check: 0 changed, 17 unchanged, 1 would change, 0 failed\n" if self.drift else \
                "host install: check: 0 changed, 18 unchanged, 0 would change, 0 failed\n"
        elif "check" in command:
            stdout = canary_json_lines("client", self.canary_fail)
            status = 1 if self.canary_fail else 0
        elif command[:3] == ["sudo", "-u", "gw"]:
            status = 255 if self.ssh_fail else 0
            stdout = "" if self.ssh_fail else "idesbx-backend\n"
            stderr = "Host key verification failed." if self.ssh_fail else ""
        elif command[-1] == "--version":
            stdout = self.version + "\n"
        retval = subprocess.CompletedProcess(command, status, stdout, stderr)
        return retval


class FakeGitHub:
    """GitHub's deploy keys per repository, as `gh api` would show them."""

    def __init__(self, runner=None):
        self.runner = runner
        self.keys = {}
        self.next_id = 1000
        self.calls = []
        self.fail = False
        self.logged_in = True

    def authenticated(self):
        retval = self.logged_in
        return retval

    def check(self):
        if self.fail:
            raise control_creds.GitHubError("gh api failed: HTTP 500")

    def list_deploy_keys(self, repository):
        self.calls.append(("list", repository))
        self.check()
        retval = list(self.keys.get(repository, []))
        return retval

    def add_deploy_key(self, repository, public_key, title, read_only):
        self.calls.append(("add", repository, title, read_only))
        self.check()
        self.next_id += 1
        retval = {"id": self.next_id, "key": control_creds.key_text(public_key), "title": title, "read_only": read_only,
                  "created_at": "2026-11-08T12:00:00Z"}
        self.keys.setdefault(repository, []).append(retval)
        return retval

    def delete_deploy_key(self, repository, key_id):
        self.calls.append(("delete", repository, key_id))
        self.check()
        retval = False
        for item in list(self.keys.get(repository, [])):
            if item["id"] == key_id:
                self.keys[repository].remove(item)
                retval = True
        return retval

    def plant(self, repository, public_key, title, read_only=True):
        """A key GitHub already holds, registered by hand or by an earlier backend."""
        self.next_id += 1
        retval = {"id": self.next_id, "key": control_creds.key_text(public_key), "title": title, "read_only": read_only,
                  "created_at": "2026-11-01T12:00:00Z"}
        self.keys.setdefault(repository, []).append(retval)
        return retval


class _NoTty:
    def isatty(self):
        retval = False
        return retval


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A state directory with a base image for this package version, the rig key, a config
    naming desk as the client host, and the driver, backends, client host and GitHub as fakes."""
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "control_ed25519").write_text("private")
    (tmp_path / "secrets" / "control_ed25519.pub").write_text(PUBKEY + "\n")
    (tmp_path / "secrets" / "sandbox_ed25519").write_text("private")
    (tmp_path / "secrets" / "sandbox_ed25519.pub").write_text(SANDBOX_PUBKEY + "\n")
    (tmp_path / "config.yaml").write_text(
        "client_host: ssh://jsw@192.168.64.10\ngit:\n  user_name: Dev\n  user_email: dev@example.invalid\n")
    images = config.images_dir()
    images.mkdir()
    name = control_images.image_name(common.package_version())
    control_images.disk_path(name).write_bytes(b"\0" * 4096)
    user_config = config.load_user_config()
    digest_user_data = render.cloudinit_document("backend", "cloudinit-image", "user-data",
                                                 control_images.seed_values(user_config, PUBKEY))
    record = control_images.wanted_record(config.load_versions(), user_config, common.architecture(), digest_user_data, "utm")
    control_images.record_path(name).write_text(record.to_yaml())
    driver = FakeDriver()
    backends = {}
    client = FakeClient()
    github = FakeGitHub()

    def fake_backend(address, vm_name, runner=None):
        if address not in backends:
            repositories = repositories_from_seed(driver.seeds.get(vm_name, {}))
            backends[address] = FakeBackend(address, vm_name, runner, repositories)
        retval = backends[address]
        return retval

    monkeypatch.setattr(control_driver, "load_driver", lambda runner=None: driver)
    monkeypatch.setattr(remote, "backend", fake_backend)
    monkeypatch.setattr(remote, "client_host", lambda user_config, runner=None: client)
    monkeypatch.setattr(control_inventory, "address_answers", lambda address, timeout=1.0: False)
    monkeypatch.setattr(control_creds, "ensure_keys",
                        lambda runner=None: (tmp_path / "secrets" / "control_ed25519", tmp_path / "secrets" / "sandbox_ed25519"))
    monkeypatch.setattr(remote, "generate_host_key", lambda comment: HOST_KEY)
    monkeypatch.setattr(control_creds, "GitHub", lambda runner=None: github)
    monkeypatch.setattr(control_creds, "require_gh", lambda: "/opt/homebrew/bin/gh")
    monkeypatch.setattr("sys.stdin", _NoTty())
    retval = {"home": tmp_path, "driver": driver, "backends": backends, "client": client, "github": github, "image": name,
              "pubkey": PUBKEY, "sandbox_pubkey": SANDBOX_PUBKEY, "host_key": HOST_KEY, "host_keys": HOST_KEYS}
    return retval
