"""Where the tool keeps its own state on a machine: one directory, ``~/.sandboxed_devenv/``.

PLAN.md ("Open decisions", state dir, decided 7 November): ``config.yaml``, ``inventory/`` with
one JSON file per project, and ``secrets/`` (mode 0700) with one file per secret and none of
them in ``config.yaml``, so configuration and secrets can be managed separately.
``SANDBOXED_DEVENV_HOME`` moves the whole directory, for tests and for a second copy of the
tool's state. Pydantic models for the user config, versions and policy arrive with M2.
"""

import os
import pathlib

HOME_DIR_NAME = ".sandboxed_devenv"
HOME_ENV_VAR = "SANDBOXED_DEVENV_HOME"
# Inside a backend VM, `backend prepare` reads this; its presence marks the backend role.
BACKEND_PROJECT_CONFIG = pathlib.Path("/etc/sandboxed-devenv/project.yaml")


def home_dir() -> pathlib.Path:
    """``~/.sandboxed_devenv/``, or ``$SANDBOXED_DEVENV_HOME`` when set."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        retval = pathlib.Path(override)
    else:
        retval = pathlib.Path.home() / HOME_DIR_NAME
    return retval


def config_file() -> pathlib.Path:
    """The user config: client host, VM name prefix, token policy; never a secret value."""
    retval = home_dir() / "config.yaml"
    return retval


def inventory_dir() -> pathlib.Path:
    """One JSON file per project: its backend and every credential (PLAN.md M3)."""
    retval = home_dir() / "inventory"
    return retval


def secrets_dir() -> pathlib.Path:
    """The rig key pair and the Claude token, one file each, mode 0700 (PLAN.md M3)."""
    retval = home_dir() / "secrets"
    return retval
