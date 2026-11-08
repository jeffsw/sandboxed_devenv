"""`test host`, `test backend` and `test end-to-end` against the fakes in conftest."""

from sandboxed_devenv import cli, control_inventory


def test_host_runs_the_drift_check_and_the_client_canary(rig, capsys):
    assert cli.main(["--dry-run", "test", "host"]) == 0
    assert "host install --check" in capsys.readouterr().out
    assert cli.main(["test", "host"]) == 0
    out = capsys.readouterr().out
    assert "ok   client host install --check: host install: check: 0 changed, 18 unchanged" in out
    assert "ok   canary client:" in out
    assert "ok   package version agrees" in out
    assert "3 ok, 0 failed" in out
    client = rig["client"]
    assert ("run", ("sudo", "/usr/local/bin/sandboxed-devenv", "host", "install", "--check")) in client.calls
    assert ("run", ("/usr/local/bin/sandboxed-devenv", "host", "check", "--json")) in client.calls
    client.drift = True
    client.canary_fail = True
    client.version = "sandboxed-devenv 0.0.9"
    assert cli.main(["test", "host"]) == 1
    out = capsys.readouterr().out
    assert "FAIL client host install --check" in out
    assert "FAIL canary client: " in out and "wayland: FAIL, expected PASS" in out
    assert "FAIL package version agrees" in out and "client host 0.0.9" in out
    assert "0 ok, 3 failed" in out


def test_backend_runs_status_and_the_backend_canary(rig, capsys):
    assert cli.main(["new", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["--dry-run", "test", "backend", "p"]) == 0
    assert "idesbx-p" in capsys.readouterr().out
    assert cli.main(["test", "backend", "p"]) == 0
    out = capsys.readouterr().out
    assert "ok   backend p ready" in out
    assert "ok   IDE backend registered for Gateway" in out
    assert "ok   canary backend:" in out
    assert "4 ok, 0 failed" in out
    backend = rig["backends"]["192.168.64.20"]
    assert ("run", ("/usr/local/bin/sandboxed-devenv", "canary", "--role", "backend", "--json")) in backend.calls
    backend.canary_fail = True
    assert cli.main(["test", "backend", "p"]) == 1
    assert "FAIL canary backend" in capsys.readouterr().out
    assert cli.main(["test", "backend", "nope"]) == 1


def test_end_to_end_makes_checks_and_removes_a_throwaway_project(rig, capsys):
    assert cli.main(["--dry-run", "test", "end-to-end"]) == 0
    assert "--disposable" in capsys.readouterr().out
    assert cli.main(["test", "end-to-end", "--memory-gib", "4"]) == 0
    out = capsys.readouterr().out
    assert "is up to date" in out
    assert "ok   base image current or built" in out
    assert "ok   new ci-" in out
    assert "ok   backend ci-" in out and "ready" in out
    assert "ok   gw on the client host reaches ci-" in out
    assert "ok   package version agrees" in out
    assert "9 ok, 0 failed" in out
    assert "test end-to-end: passed" in out
    assert control_inventory.list_projects() == []
    assert rig["driver"].vms == {}
    created = [call for call in rig["driver"].calls if call[0] == "create"]
    assert len(created) == 1 and created[0][1].startswith("idesbx-ci-")
    assert ("run", ("sudo", "-u", "gw", "ssh", "-o", "BatchMode=yes", created[0][1][len("idesbx-"):], "hostname")) in rig["client"].calls


def test_end_to_end_keeps_the_project_on_failure_or_with_keep(rig, capsys):
    rig["client"].ssh_fail = True
    assert cli.main(["test", "end-to-end"]) == 1
    out = capsys.readouterr()
    assert "FAIL gw on the client host reaches ci-" in out.out and "Host key verification failed" in out.out
    assert "kept for inspection" in out.err
    kept = control_inventory.list_projects()
    assert len(kept) == 1 and kept[0].disposable is True
    assert cli.main(["rm", kept[0].project, "--yes"]) == 0
    rig["client"].ssh_fail = False
    assert cli.main(["test", "end-to-end", "--keep"]) == 0
    assert "kept (--keep)" in capsys.readouterr().out
    assert len(control_inventory.list_projects()) == 1
