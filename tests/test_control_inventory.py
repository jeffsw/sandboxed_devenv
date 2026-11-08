"""The inventory: records round-trip, names are checked, addresses come from the pool."""

import json

import pytest

from sandboxed_devenv import config, control_inventory


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    retval = tmp_path
    return retval


def record(project="p", address="192.168.64.20", **overrides) -> control_inventory.ProjectRecord:
    fields = dict(project=project, vm_name=f"idesbx-{project}", driver="utm", image="backend-0.1.0", package_version="0.1.0",
                  address=address, cpus=4, memory_gib=8, disk_gib=32, created=control_inventory.now())
    fields.update(overrides)
    retval = control_inventory.ProjectRecord(**fields)
    return retval


def test_round_trip_and_listing(home):
    assert control_inventory.list_projects() == []
    assert control_inventory.load("p") is None
    with pytest.raises(control_inventory.InventoryError):
        control_inventory.require("p")
    saved = record(repos=[control_inventory.RepositoryRecord(name="jeffsw/a", fingerprint="SHA256:x", public_key="ssh-ed25519 AAAA")])
    path = control_inventory.save(saved)
    assert path == home / "inventory" / "p.json"
    assert json.loads(path.read_text())["repos"][0]["state"] == "unregistered"
    assert control_inventory.require("p") == saved
    control_inventory.save(record("b", "192.168.64.21", disposable=True, cloned_from="p"))
    assert [item.project for item in control_inventory.list_projects()] == ["b", "p"]
    assert control_inventory.used_addresses() == {"192.168.64.20", "192.168.64.21"}
    assert control_inventory.delete("b") is True
    assert control_inventory.delete("b") is False
    assert not list((home / "inventory").glob(".*"))


def test_project_names():
    assert control_inventory.valid_project_name("ide_sandbox")
    assert control_inventory.valid_project_name("x.y-z")
    assert not control_inventory.valid_project_name("has space")
    assert not control_inventory.valid_project_name("a/b")
    with pytest.raises(Exception):
        record("bad name")


def test_allocate_address_skips_the_inventory_the_fixed_hosts_and_answering_hosts(home):
    pool = control_inventory.utm_pool(config.UserConfig())
    assert pool.build_address == "192.168.64.19" and "192.168.64.10" in pool.reserved
    reports = []
    assert control_inventory.allocate_address(pool, probe=lambda address: False, report=reports.append) == "192.168.64.20"
    control_inventory.save(record("p", "192.168.64.20"))
    answering = {"192.168.64.21"}
    chosen = control_inventory.allocate_address(pool, probe=lambda address: address in answering, report=reports.append)
    assert chosen == "192.168.64.22"
    assert reports == ["address 192.168.64.21 answers on port 22 but is not in the inventory; skipping it"]
    tiny = control_inventory.utm_pool(config.UserConfig(utm=config.UtmConfig(subnet="192.168.64.0/30", address_pool_start="192.168.64.2")))
    control_inventory.save(record("q", "192.168.64.2"))
    with pytest.raises(control_inventory.InventoryError):
        control_inventory.allocate_address(tiny, probe=lambda address: False, report=reports.append)


def test_address_answers_is_false_for_nothing_listening():
    assert control_inventory.address_answers("127.0.0.1", timeout=0.2) in (True, False)
    assert control_inventory.address_answers("192.0.2.1", timeout=0.05) is False
