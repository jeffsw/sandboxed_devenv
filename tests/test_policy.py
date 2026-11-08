"""policy.yaml: the model, the user's override, and the nftables ruleset and desk routes rendered from it."""

import pytest
import yaml

from sandboxed_devenv import cli, config, render

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA test"


def test_package_policy_loads_and_splits_the_subnet(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    policy = config.load_policy()
    assert config.policy_file() == config.package_policy_file()
    assert policy.vm_subnet == "192.168.64.0/24"
    assert policy.enforce is False
    assert policy.subnet_halves() == ("192.168.64.0/25", "192.168.64.128/25")
    assert "140.82.112.0/20" in policy.backend.cidrs
    assert policy.backend.ports == [22, 443]
    assert "download.jetbrains.com" in policy.client.hosts


def test_user_policy_overrides_the_package_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    (tmp_path / "policy.yaml").write_text("uplink_interface: wlp1s0\nenforce: true\nbackend:\n  cidrs: [10.0.0.0/8]\n")
    policy = config.load_policy()
    assert config.policy_file() == tmp_path / "policy.yaml"
    assert policy.uplink_interface == "wlp1s0"
    assert policy.enforce is True
    assert policy.backend.cidrs == ["10.0.0.0/8"]
    assert policy.client.cidrs == []
    (tmp_path / "policy.yaml").write_text("backend:\n  cidrs: [not-a-network]\n")
    with pytest.raises(Exception):
        config.load_policy()
    (tmp_path / "policy.yaml").write_text("vm_subnet: 192.168.64.0/24\nunknown: 1\n")
    with pytest.raises(Exception):
        config.load_policy()


def test_nftables_renders_from_the_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    values = render.default_values(config.UserConfig(), CONTROL_PUBKEY=PUBKEY)
    assert values["VM_SUBNET"] == "192.168.64.0/24"
    assert values["BACKEND_ALLOWED"].startswith("elements = { 140.82.112.0/20, ")
    assert values["CLIENT_ALLOWED"] == "# no elements"
    assert values["BACKEND_PORTS"] == "{ 22, 443 }"
    assert values["SANDBOX_USER"] == "gw"
    assert values["SANDBOX_UID"].isdigit()
    entry = render.load_manifest()["host"]["etc__nftables.conf.tmpl"]
    text = render.render_entry("host", "etc__nftables.conf.tmpl", entry, values)
    assert "define vm_subnet = 192.168.64.0/24" in text
    assert 'define uplink = "en*"' in text
    assert "ip saddr $vm_subnet ip daddr @backend_allowed tcp dport { 22, 443 } accept" in text
    assert "meta skuid != $sandbox_uid accept" in text
    assert f"define sandbox_uid = {render.sandbox_uid()}" in text
    assert "download.jetbrains.com" in text
    assert "@@" not in text
    assert render.nft_elements([]) == "# no elements"
    assert render.nft_ports([443]) == "{ 443 }"


def test_desk_network_config_routes_both_halves_through_the_mac(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    user_config = config.UserConfig(client_host="ssh://jsw@192.168.64.10")
    values = render.default_values(user_config, CONTROL_PUBKEY=PUBKEY, **render.sample_host_key())
    assert values["CLIENT_HOST_USER"] == "jsw"
    network = yaml.safe_load(render.cloudinit_document("host", "cloudinit", "network-config", values))
    ethernet = network["ethernets"]["enp0s1"]
    assert ethernet["addresses"] == ["192.168.64.10/24"]
    routes = [(route["to"], route["via"]) for route in ethernet["routes"]]
    assert routes == [("default", "192.168.64.1"), ("192.168.64.0/25", "192.168.64.1"), ("192.168.64.128/25", "192.168.64.1")]
    user_data = yaml.safe_load(render.cloudinit_document("host", "cloudinit", "user-data", values))
    assert user_data["users"][0]["name"] == "jsw"
    assert render.client_host_user(config.UserConfig()) == render.DEFAULT_CLIENT_HOST_USER


def test_doctor_names_the_policy_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SANDBOXED_DEVENV_HOME", str(tmp_path))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "policy: " in out
    assert "the package's" in out
    assert "0 projects" in out
