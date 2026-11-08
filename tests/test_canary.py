"""The canary stays standard-library only and runnable on its own; its JSON output parses."""

import json
import pathlib
import subprocess
import sys

from sandboxed_devenv import canary, cli

CANARY_FILE = pathlib.Path(canary.__file__)


def test_runs_as_a_standalone_script_without_the_package():
    # -I: no script directory or current directory on sys.path, so a package import would fail.
    completed = subprocess.run([sys.executable, "-I", str(CANARY_FILE), "--help"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "--role" in completed.stdout


def test_runs_as_a_module():
    completed = subprocess.run([sys.executable, "-m", "sandboxed_devenv.canary", "--help"], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_subcommand_requires_a_role():
    try:
        cli.main(["canary"])
    except SystemExit as raised:
        assert raised.code == 2
    else:
        raise AssertionError("no usage error")


def test_json_report_and_summary(capsys):
    canary.set_output_json(True)
    try:
        canary.report("PASS", "a probe", "fine")
        canary.report("FAIL", "another", "not fine")
        canary.report("INFO", "third", "note")
        counts = canary.summary()
    finally:
        canary.set_output_json(False)
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0]) == {"status": "PASS", "name": "a probe", "detail": "fine"}
    assert json.loads(lines[1])["status"] == "FAIL"
    assert counts == {"pass": 1, "fail": 1, "info": 1}


def test_text_report(capsys):
    canary.set_output_json(False)
    canary.report("PASS", "a probe", "fine")
    assert capsys.readouterr().out == "PASS a probe: fine\n"
    canary.RESULTS.clear()


def test_expectations_file_covers_both_roles_with_known_statuses():
    for role in ("client", "backend"):
        expected = canary.expected_for(role)
        assert expected, role
        for pattern, allowed in expected.items():
            assert allowed and set(allowed) <= set(canary.STATUSES), (role, pattern, allowed)
        assert expected["sandboxed-devenv command"] == ["PASS"]
    assert canary.expected_for("client")["wayland"] == ["PASS"]
    assert canary.expected_for("backend")["ssh to github.com"] == ["INFO"]


def test_compare_reports_missing_wrong_and_uncovered_failures(tmp_path):
    results = [("PASS", "wayland", "answers"), ("INFO", "X11 /tmp/.X11-unix/X9", "own"), ("PASS", "X11 /tmp/.X11-unix/X0", "refused"),
               ("FAIL", "agent /run/x", "lists 1 key"), ("FAIL", "dbus session (real user)", "usable")]
    expected = {"wayland": ["PASS"], "X11*": ["PASS", "INFO"], "agent*": ["PASS"], "sandboxed-devenv command": ["PASS"]}
    mismatches = canary.compare(results, expected)
    assert mismatches == [
        "agent /run/x: FAIL, expected PASS (lists 1 key)",
        "sandboxed-devenv command: no such probe ran; expected PASS",
        "dbus session (real user): FAIL (usable)",
    ]
    assert canary.compare([("PASS", "wayland", "")], {"wayland": "PASS"}) == []
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"wayland": "PASS", "agent*": ["PASS", "INFO"]}))
    loaded = canary.expected_for("client", str(golden))
    assert loaded == {"wayland": ["PASS"], "agent*": ["PASS", "INFO"]}


def test_json_lines_parse_back_into_a_report():
    text = "\n".join([
        json.dumps({"canary": {"role": "backend", "user": "dev", "host_home": None}}),
        json.dumps({"status": "PASS", "name": "wayland", "detail": "no socket"}),
        "host check: a notice that is not JSON",
        json.dumps({"expect": {"source": "x", "mismatches": ["wayland: FAIL"]}}),
        json.dumps({"summary": {"pass": 1, "fail": 0, "info": 0}}),
    ])
    report = canary.parse_json_lines(text)
    assert report["canary"]["role"] == "backend"
    assert report["probes"] == [("PASS", "wayland", "no socket")]
    assert report["mismatches"] == ["wayland: FAIL"]
    assert report["summary"]["pass"] == 1
    assert report["other"] == ["host check: a notice that is not JSON"]
    assert canary.parse_json_lines("")["summary"] is None


def test_expect_flag_is_offered_everywhere():
    completed = subprocess.run([sys.executable, "-I", str(CANARY_FILE), "--help"], capture_output=True, text=True)
    assert "--expect" in completed.stdout
