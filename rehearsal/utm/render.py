#!/usr/bin/env python3
"""
Render the backend's cloud-init seed directory and assemble desk's (summary, 4.9).

Run from rehearsal/utm, normally by make-seed.sh after `sandboxed-devenv render`. The backend
template, backend/user-data.tmpl, has its @@NAME@@ placeholders filled here until M2 moves it
into the package; the desk user-data is the package's output, <checkout>/build/host/cloudinit/
user-data, copied beside desk's static meta-data and network-config. Standard library only, so
it also runs on a CI runner.

Inputs:
  ~/.sandboxed_devenv/secrets/gw_ed25519.pub   the rig key's public half (make-seed.sh creates
                                               the pair; SANDBOXED_DEVENV_HOME moves the directory)
  ~/.sandboxed_devenv/secrets/claude_oauth_token
                                               optional; a token from `claude setup-token` on the host
  IDEA_VERSION, GIT_USER_NAME, GIT_USER_EMAIL  environment, with defaults below
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys

CHECKOUT = pathlib.Path(__file__).resolve().parent.parent.parent
SECRETS = pathlib.Path(os.environ.get('SANDBOXED_DEVENV_HOME') or pathlib.Path.home() / '.sandboxed_devenv') / 'secrets'
PACKAGE_DESK_USER_DATA = CHECKOUT / 'build' / 'host' / 'cloudinit' / 'user-data'


def git_global(key: str) -> str:
    """A value from the host's global git config, or an empty string."""
    try:
        done = subprocess.run(['git', 'config', '--global', key], capture_output=True, text=True)
    except FileNotFoundError:
        return ''
    retstr = done.stdout.strip() if done.returncode == 0 else ''
    return retstr


def settings_json() -> str:
    """Claude Code's settings for the backend, with the token when the host provides one."""
    settings = {'theme': 'dark'}
    token_path = SECRETS / 'claude_oauth_token'
    if token_path.is_file():
        token = token_path.read_text().strip()
        if token:
            settings['env'] = {'CLAUDE_CODE_OAUTH_TOKEN': token}
    retstr = json.dumps(settings, indent=2) + '\n'
    return retstr


def values() -> dict[str, str]:
    """Every placeholder the backend template may use."""
    retdict = {
        'GW_PUBKEY': (SECRETS / 'gw_ed25519.pub').read_text().strip(),
        'IDEA_VERSION': os.environ.get('IDEA_VERSION', '2026.2.3'),
        'GIT_USER_NAME': os.environ.get('GIT_USER_NAME') or git_global('user.name') or 'Developer',
        'GIT_USER_EMAIL': os.environ.get('GIT_USER_EMAIL') or git_global('user.email') or 'developer@example.invalid',
        # The canary is the package's module, still copied in as a script until M2 installs the package.
        'CANARY_B64': base64.b64encode((CHECKOUT / 'sandboxed_devenv' / 'canary.py').read_bytes()).decode(),
        'CLAUDE_SETTINGS_B64': base64.b64encode(settings_json().encode()).decode(),
    }
    return retdict


def seed_dir(vm: str) -> pathlib.Path:
    """An empty build/<vm>-seed/ with the VM's static meta-data and network-config."""
    out = pathlib.Path('build') / f'{vm}-seed'
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ('meta-data', 'network-config'):
        shutil.copy(pathlib.Path(vm, name), out / name)
    return out


def render_backend(vals: dict[str, str]) -> pathlib.Path:
    """Fill the backend template; returns the seed directory."""
    out = seed_dir('backend')
    text = pathlib.Path('backend', 'user-data.tmpl').read_text()
    for key, val in vals.items():
        text = text.replace(f'@@{key}@@', val)
    if '@@' in text:
        raise SystemExit('unfilled placeholder in backend/user-data.tmpl')
    (out / 'user-data').write_text(text)
    return out


def assemble_desk() -> pathlib.Path:
    """The package's rendered desk user-data beside desk's static files."""
    if not PACKAGE_DESK_USER_DATA.is_file():
        raise SystemExit(f'{PACKAGE_DESK_USER_DATA} is missing: run `sandboxed-devenv render` first (make-seed.sh does)')
    out = seed_dir('desk')
    shutil.copy(PACKAGE_DESK_USER_DATA, out / 'user-data')
    return out


def main() -> int:
    if not (SECRETS / 'gw_ed25519.pub').is_file():
        print(f'{SECRETS}/gw_ed25519.pub is missing; make-seed.sh creates the pair', file=sys.stderr)
        return 1
    vals = values()
    print(f'rendered {render_backend(vals)}')
    print(f'assembled {assemble_desk()} from {PACKAGE_DESK_USER_DATA}')
    has_token = 'env' in json.loads(settings_json())
    print(f'Claude Code token from {SECRETS}/claude_oauth_token included in backend settings' if has_token
          else f'no {SECRETS}/claude_oauth_token: Claude Code will ask to log in on the backend')
    print(f"git identity for the VM: {vals['GIT_USER_NAME']} <{vals['GIT_USER_EMAIL']}>")
    return 0


if __name__ == '__main__':
    sys.exit(main())
