"""The command tree matches PLAN.md's CLI surface, `--version` matches pyproject.toml, exit codes hold."""

import argparse
import tomllib

import pytest

from sandboxed_devenv import cli

# Every command in PLAN.md, "CLI surface", as a path through the tree. A new command goes in
# both places; this test is what notices when only one of them changed.
PLANNED_COMMANDS = [
    ("doctor",),
    ("render",),
    ("canary",),
    ("image", "build"),
    ("image", "ls"),
    ("image", "rm"),
    ("new",),
    ("ls",),
    ("start",),
    ("stop",),
    ("rm",),
    ("clone",),
    ("resources",),
    ("client-host", "new"),
    ("client-host", "rm"),
    ("client-host", "ssh"),
    ("repo", "add"),
    ("repo", "rm"),
    ("repo", "ls"),
    ("ssh",),
    ("serial-console",),
    ("creds", "ls"),
    ("creds", "rotate"),
    ("creds", "audit"),
    ("test", "end-to-end"),
    ("test", "host"),
    ("test", "backend"),
    ("host", "install"),
    ("host", "check"),
    ("client", "start"),
    ("client", "session"),
    ("client", "exec"),
    ("connect", "add"),
    ("connect", "rm"),
    ("connect", "ls"),
    ("backend", "prepare"),
    ("backend", "status"),
    ("backend", "repo", "add"),
    ("backend", "repo", "rm"),
    ("backend", "repo", "rekey"),
    ("backend", "repo", "ls"),
]


def leaf_commands(parser: argparse.ArgumentParser, prefix: tuple = ()) -> list[tuple]:
    """Every path through the subparser tree that ends at a command with a handler."""
    retlist = []
    subparser_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    for action in subparser_actions:
        for name, subparser in action.choices.items():
            path = prefix + (name,)
            nested = leaf_commands(subparser, path)
            if nested:
                retlist.extend(nested)
            else:
                retlist.append(path)
    return retlist


def test_command_tree_matches_the_plan():
    parser = cli.build_parser()
    assert sorted(leaf_commands(parser)) == sorted(PLANNED_COMMANDS)


def test_every_command_has_a_handler():
    parser = cli.build_parser()
    for path in PLANNED_COMMANDS:
        subparser = parser
        for name in path:
            action = [a for a in subparser._actions if isinstance(a, argparse._SubParsersAction)][0]
            subparser = action.choices[name]
        assert "handler" in subparser._defaults, path


def test_root_help_lists_every_top_level_command(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    top_level = set()
    for path in PLANNED_COMMANDS:
        top_level.add(path[0])
    for name in top_level:
        assert name in help_text, name


def test_version_matches_pyproject(checkout, capsys):
    with open(checkout / "pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"sandboxed-devenv {pyproject['project']['version']}"


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2


def test_group_without_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as raised:
        cli.main(["host"])
    assert raised.value.code == 2


def test_stub_exits_1_and_names_its_milestone(capsys):
    status = cli.main(["creds", "ls"])
    assert status == 1
    assert "M3" in capsys.readouterr().err


def test_doctor_runs(capsys):
    status = cli.main(["doctor"])
    assert status == 0
    out = capsys.readouterr().out
    assert "roles:" in out
    assert "inventory:" in out


def test_render_dry_run_names_the_wheel_build(capsys):
    status = cli.main(["--dry-run", "render"])
    assert status == 0
    assert "--wheel" in capsys.readouterr().out
