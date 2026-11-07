"""`sandboxed-devenv render`: build every artifact without touching a VM; CI's entry point.

M0 builds the wheel, which every VM's cloud-init will carry. M2 adds the templates, ported from
rehearsal/utm/render.py, and the mise `render` task switches over then.
"""

import pathlib
import shlex
import shutil
import subprocess
import sys

CHECKOUT_MARKER = 'name = "sandboxed-devenv"'


def register(subparsers):
    parser = subparsers.add_parser(
        "render",
        help="render every artifact and build the wheel, without touching a VM (any role; CI's entry point)",
    )
    parser.add_argument(
        "--out",
        default="build",
        help="output directory, relative to the checkout unless absolute (default: build/)",
    )
    parser.set_defaults(handler=cmd_render)


def find_checkout(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """The repository root above `start` (default: the working directory), or None outside a checkout."""
    retval = None
    here = (start or pathlib.Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and CHECKOUT_MARKER in pyproject.read_text():
            retval = candidate
            break
    return retval


def wheel_build_command(out_dir: pathlib.Path) -> list[str]:
    """uv when present (pinned by mise), else the `build` package from the dev group."""
    if shutil.which("uv"):
        retval = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        retval = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    return retval


def cmd_render(args) -> int:
    checkout = find_checkout()
    if checkout is None:
        print("sandboxed-devenv render: run it inside a checkout of the repository (no pyproject.toml found)",
              file=sys.stderr)
        retval = 1
        return retval
    out_dir = pathlib.Path(args.out)
    if not out_dir.is_absolute():
        out_dir = checkout / out_dir
    command = wheel_build_command(out_dir)
    if args.dry_run:
        print(f"would run in {checkout}: {shlex.join(command)}")
        retval = 0
        return retval
    out_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, cwd=checkout)
    if completed.returncode != 0:
        retval = 1
        return retval
    wheels = sorted(out_dir.glob("sandboxed_devenv-*.whl"))
    if wheels:
        print(f"render: wheel {wheels[-1]}")
    retval = 0
    return retval
