"""The idempotent-step machinery shared by `host install` and `backend prepare`.

A step inspects the machine, says what differs, and changes it only when the context applies.
Every step reports one of four outcomes, and a file is written atomically with its owner,
group and mode; `--check` is the same code with ``apply`` off.
"""

import dataclasses
import grp
import os
import pathlib
import pwd
import stat
import subprocess

CHANGED = "changed"
UNCHANGED = "unchanged"
WOULD_CHANGE = "would change"
FAILED = "failed"
STATUSES = (CHANGED, UNCHANGED, WOULD_CHANGE, FAILED)


@dataclasses.dataclass
class Context:
    apply: bool
    values: dict[str, str] = dataclasses.field(default_factory=dict)
    changed: set = dataclasses.field(default_factory=set)
    # The command's name in each printed line: "host install" or "backend prepare".
    command: str = ""


@dataclasses.dataclass
class Outcome:
    label: str
    status: str
    detail: str = ""

    def line(self, command: str) -> str:
        retval = f"{command}: {self.label}: {self.status}"
        if self.detail:
            retval += f" ({self.detail})"
        return retval


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    retval = subprocess.run(command, text=True, **kwargs)
    return retval


def outcome(ctx: Context, label: str, detail: str, differs: bool, action, unchanged_detail: str = "") -> Outcome:
    """The shape of every step: nothing differs, unchanged; differs without apply, would change; with
    apply, run the action and report changed, or failed with the error."""
    if not differs:
        retval = Outcome(label, UNCHANGED, unchanged_detail)
    elif not ctx.apply:
        retval = Outcome(label, WOULD_CHANGE, detail)
    else:
        try:
            action()
            retval = Outcome(label, CHANGED, detail)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
            retval = Outcome(label, FAILED, f"{detail}: {error}")
    return retval


def read_text(path: pathlib.Path) -> str | None:
    retval = None
    try:
        retval = path.read_text()
    except OSError:
        pass
    return retval


def ensure_parents(dest: pathlib.Path, uid: int, gid: int) -> None:
    """Missing parent directories, owned like the file; `.ssh` closed, the rest world-readable."""
    missing = []
    parent = dest.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        os.chown(directory, uid, gid)
        directory.chmod(0o700 if directory.name == ".ssh" else 0o755)


def sync_file(dest: pathlib.Path, content: bytes, owner: str, group: str, mode: int, apply: bool) -> Outcome:
    """Content, owner, group and mode as given, written atomically; reports what differed."""
    label = f"file {dest}"
    try:
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as error:
        if apply:
            retval = Outcome(label, FAILED, f"no such user or group: {error}")
        else:
            retval = Outcome(label, WOULD_CHANGE, f"created once {owner}:{group} exist")
        return retval
    differences = []
    if not dest.exists():
        differences.append("created")
    else:
        status = dest.stat()
        if dest.read_bytes() != content:
            differences.append("content")
        if (status.st_uid, status.st_gid) != (uid, gid):
            differences.append("owner")
        if stat.S_IMODE(status.st_mode) != mode:
            differences.append("mode")
    if not differences:
        retval = Outcome(label, UNCHANGED, f"{owner}:{group} {mode:04o}")
        return retval
    detail = ", ".join(differences)
    if not apply:
        retval = Outcome(label, WOULD_CHANGE, detail)
        return retval
    try:
        ensure_parents(dest, uid, gid)
        temporary = dest.with_name(f".{dest.name}.sandboxed-devenv.tmp")
        temporary.write_bytes(content)
        os.chown(temporary, uid, gid)
        temporary.chmod(mode)
        os.replace(temporary, dest)
        retval = Outcome(label, CHANGED, detail)
    except OSError as error:
        retval = Outcome(label, FAILED, f"{detail}: {error}")
    return retval


def remove_file(path: pathlib.Path, apply: bool) -> Outcome:
    label = f"file {path}"
    if not path.exists():
        retval = Outcome(label, UNCHANGED, "absent")
    elif not apply:
        retval = Outcome(label, WOULD_CHANGE, "removed")
    else:
        try:
            path.unlink()
            retval = Outcome(label, CHANGED, "removed")
        except OSError as error:
            retval = Outcome(label, FAILED, str(error))
    return retval


def tally(outcomes: list[Outcome]) -> dict[str, int]:
    retdict = {}
    for status in STATUSES:
        retdict[status] = 0
    for result in outcomes:
        retdict[result.status] += 1
    return retdict


def summary_line(command: str, mode: str, counts: dict[str, int]) -> str:
    retval = (f"{command}: {mode}: {counts[CHANGED]} changed, {counts[UNCHANGED]} unchanged, "
              f"{counts[WOULD_CHANGE]} would change, {counts[FAILED]} failed")
    return retval


def exit_status(counts: dict[str, int], check: bool) -> int:
    """1 when a step failed, or under --check when anything would change."""
    if counts[FAILED]:
        retval = 1
    elif check and counts[WOULD_CHANGE]:
        retval = 1
    else:
        retval = 0
    return retval
