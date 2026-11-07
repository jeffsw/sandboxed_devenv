"""`sandboxed-devenv backend ...`: what runs inside a backend VM (backend role, M2)."""

from sandboxed_devenv import common

STAGES = ["image", "project"]


def register(subparsers):
    backend = common.add_group(subparsers, "backend", "inside a backend VM: first boot, status, repositories (backend)")

    prepare = backend.add_parser(
        "prepare",
        help="first boot: host keys, git identity, Claude settings, then `backend repo add` for each listed repository",
    )
    prepare.add_argument(
        "--stage",
        choices=STAGES,
        default="project",
        help="`image` while building the base image, `project` at a project's first boot (default)",
    )
    prepare.set_defaults(handler=cmd_prepare)

    status = backend.add_parser(
        "status",
        help="cloud-init done, IDE backend registered, package version, repositories, SSH host public keys",
    )
    status.add_argument("--json", action="store_true", help="machine-readable, for the control side")
    status.set_defaults(handler=cmd_status)

    repo = common.add_group(backend, "repo", "the VM side of `repo`: key pairs, alias stanzas, checkouts")

    add = repo.add_parser("add", help="mint a key pair and alias stanza, clone, print the public key as JSON")
    add.add_argument("repository", metavar="OWNER/REPO")
    add.add_argument("--no-clone", action="store_true", help="mint and bind the key but do not clone")
    add.set_defaults(handler=cmd_repo_add)

    rm = repo.add_parser("rm", help="delete the key pair and alias stanza; the checkout stays unless asked")
    rm.add_argument("repository", metavar="OWNER/REPO")
    rm.add_argument("--delete-checkout", action="store_true", help="also delete the checkout")
    rm.set_defaults(handler=cmd_repo_rm)

    rekey = repo.add_parser("rekey", help="a fresh key pair replacing the repository's, printed as JSON")
    rekey.add_argument("repository", metavar="OWNER/REPO")
    rekey.set_defaults(handler=cmd_repo_rekey)

    ls = repo.add_parser("ls", help="the repositories and their key fingerprints")
    ls.add_argument("--json", action="store_true", help="machine-readable, for the control side")
    ls.set_defaults(handler=cmd_repo_ls)


def cmd_prepare(args) -> int:
    retval = common.not_implemented("backend prepare", "M2")
    return retval


def cmd_status(args) -> int:
    retval = common.not_implemented("backend status", "M2")
    return retval


def cmd_repo_add(args) -> int:
    retval = common.not_implemented("backend repo add", "M2")
    return retval


def cmd_repo_rm(args) -> int:
    retval = common.not_implemented("backend repo rm", "M2")
    return retval


def cmd_repo_rekey(args) -> int:
    retval = common.not_implemented("backend repo rekey", "M2")
    return retval


def cmd_repo_ls(args) -> int:
    retval = common.not_implemented("backend repo ls", "M2")
    return retval
