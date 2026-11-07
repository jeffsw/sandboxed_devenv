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
