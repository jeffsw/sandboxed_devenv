"""Repositories and their credentials: `repo ...` and `creds ...` (control role, M3).

One deploy key pair per repository inside the backend, registered on GitHub from here and
revoked before anything is deleted; the inventory keeps every key's GitHub id and state.
From M2 this module also mints the rig key pair, the one credential every VM creation needs.
"""

import pathlib
import socket

from sandboxed_devenv import common, remote

ALL_REPOSITORIES = "ALL"
RIG_KEY_COMMENT_USER = "gw"


def ensure_rig_key(runner: common.Runner | None = None) -> pathlib.Path:
    """The rig key pair in the secrets directory, minted on first use: ed25519, no passphrase, so
    `image build` and `new` can log into a VM they just booted. The client sandbox gets the same
    pair as /home/gw/.ssh/vm_ed25519 (`host install`), which is why the comment names gw."""
    runner = runner or common.Runner()
    retval = remote.rig_key_path()
    if not retval.is_file():
        secrets = retval.parent
        secrets.mkdir(parents=True, exist_ok=True)
        secrets.chmod(0o700)
        comment = f"{RIG_KEY_COMMENT_USER}@{socket.gethostname()}"
        completed = runner.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(retval)])
        if completed.returncode != 0:
            raise remote.RemoteError(f"ssh-keygen failed with status {completed.returncode}")
        print(f"rig key pair created: {retval} (comment {comment}); every VM made from now on trusts it")
    return retval


def rig_public_key() -> str:
    """The public half, one line, for cloud-init's ssh_authorized_keys."""
    path = remote.rig_key_path().with_suffix(".pub")
    if not path.is_file():
        raise remote.RemoteError(f"no public key at {path} beside the rig key")
    retval = path.read_text().strip()
    return retval


def register(subparsers):
    repo = common.add_group(subparsers, "repo", "a project's repositories and their deploy keys (control)")

    add = repo.add_parser(
        "add",
        help="mint a key pair in the backend, register it on GitHub, record it, clone to ~/proj/PROJECT/REPO",
    )
    add.add_argument("project", metavar="PROJECT")
    add.add_argument("repository", metavar="OWNER/REPO")
    add.add_argument(
        "--deploy-key-grants-repo-write",
        action="store_true",
        help="register the deploy key with write access (the default key is read-only)",
    )
    add.add_argument("--no-clone", action="store_true", help="register the key but do not clone")
    add.set_defaults(handler=cmd_repo_add)

    rm = repo.add_parser("rm", help="revoke the key on GitHub, then remove it and its alias in the backend")
    rm.add_argument("project", metavar="PROJECT")
    rm.add_argument("repository", metavar="OWNER/REPO")
    rm.add_argument("--delete-checkout", action="store_true", help="also delete ~/proj/PROJECT/REPO")
    rm.set_defaults(handler=cmd_repo_rm)

    ls = repo.add_parser("ls", help="the project's repositories with key fingerprint, access and age")
    ls.add_argument("project", metavar="PROJECT")
    ls.set_defaults(handler=cmd_repo_ls)

    creds = common.add_group(subparsers, "creds", "every credential: list, rotate, audit (control)")

    creds_ls = creds.add_parser("ls", help="every credential, scope, age, where it lives, how it is revoked")
    creds_ls.add_argument("project", metavar="PROJECT", nargs="?", help="one project, or every project")
    creds_ls.set_defaults(handler=cmd_creds_ls)

    rotate = creds.add_parser("rotate", help="regenerate inside the backend, re-register, update the inventory")
    rotate.add_argument("project", metavar="PROJECT")
    rotate.add_argument(
        "--deploy-key",
        metavar="OWNER/REPO",
        nargs="?",
        const=ALL_REPOSITORIES,
        help="rotate one repository's deploy key, or every repository's when no OWNER/REPO is given",
    )
    rotate.add_argument("--claude-token", action="store_true", help="replace the Claude token from the secrets dir")
    rotate.set_defaults(handler=cmd_creds_rotate)

    audit = creds.add_parser(
        "audit",
        help="compare the inventory with GitHub's deploy keys: retry pending revocations, report unclaimed keys",
    )
    audit.add_argument("--fix", action="store_true", help="delete the keys GitHub holds that no project claims")
    audit.set_defaults(handler=cmd_creds_audit)


def cmd_repo_add(args) -> int:
    retval = common.not_implemented("repo add", "M3")
    return retval


def cmd_repo_rm(args) -> int:
    retval = common.not_implemented("repo rm", "M3")
    return retval


def cmd_repo_ls(args) -> int:
    retval = common.not_implemented("repo ls", "M3")
    return retval


def cmd_creds_ls(args) -> int:
    retval = common.not_implemented("creds ls", "M3")
    return retval


def cmd_creds_rotate(args) -> int:
    retval = common.not_implemented("creds rotate", "M3")
    return retval


def cmd_creds_audit(args) -> int:
    retval = common.not_implemented("creds audit", "M3")
    return retval
