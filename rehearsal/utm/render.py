#!/usr/bin/env python3
"""
Render the cloud-init seed directories for the rehearsal VMs (summary, 4.9).

Run from rehearsal/utm, normally by make-seed.sh. For each VM it fills the @@NAME@@
placeholders of <vm>/user-data.tmpl and writes build/<vm>-seed/{user-data,meta-data,
network-config}. Standard library only, so it also runs on a CI runner.

Inputs:
  keys/gw_ed25519(.pub)        the client sandbox's key pair (make-seed.sh creates it)
  keys/claude_oauth_token      optional; a token from `claude setup-token` on the host
  GATEWAY_VERSION, IDEA_VERSION, DESK_PASSWORD, GIT_USER_NAME, GIT_USER_EMAIL
                               environment, with defaults below
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys

VMS = ('desk', 'backend')


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
    token_path = pathlib.Path('keys/claude_oauth_token')
    if token_path.is_file():
        token = token_path.read_text().strip()
        if token:
            settings['env'] = {'CLAUDE_CODE_OAUTH_TOKEN': token}
    retstr = json.dumps(settings, indent=2) + '\n'
    return retstr


def values() -> dict[str, str]:
    """Every placeholder the templates may use."""
    gateway_version = os.environ.get('GATEWAY_VERSION', '2026.2.2')
    retdict = {
        'GW_PUBKEY': pathlib.Path('keys/gw_ed25519.pub').read_text().strip(),
        'GW_PRIVKEY_B64': base64.b64encode(pathlib.Path('keys/gw_ed25519').read_bytes()).decode(),
        'GATEWAY_VERSION': gateway_version,
        # Gateway keeps its settings under ~/.config/JetBrains/JetBrainsGateway<major.minor>/
        'GATEWAY_BRANCH': '.'.join(gateway_version.split('.')[:2]),
        'IDEA_VERSION': os.environ.get('IDEA_VERSION', '2026.2.3'),
        'DESK_PASSWORD': os.environ.get('DESK_PASSWORD', 'rehearsal'),
        'GIT_USER_NAME': os.environ.get('GIT_USER_NAME') or git_global('user.name') or 'Developer',
        'GIT_USER_EMAIL': os.environ.get('GIT_USER_EMAIL') or git_global('user.email') or 'developer@example.invalid',
        'CANARY_B64': base64.b64encode(pathlib.Path('../canary.py').read_bytes()).decode(),
        'CLAUDE_SETTINGS_B64': base64.b64encode(settings_json().encode()).decode(),
    }
    return retdict


def render(vm: str, vals: dict[str, str]) -> pathlib.Path:
    """Fill one VM's template and copy its static files; returns the seed directory."""
    out = pathlib.Path('build') / f'{vm}-seed'
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    text = pathlib.Path(vm, 'user-data.tmpl').read_text()
    for key, val in vals.items():
        text = text.replace(f'@@{key}@@', val)
    if '@@' in text:
        raise SystemExit(f'unfilled placeholder in {vm}/user-data.tmpl')
    (out / 'user-data').write_text(text)
    for name in ('meta-data', 'network-config'):
        shutil.copy(pathlib.Path(vm, name), out / name)
    return out


def main() -> int:
    if not pathlib.Path('keys/gw_ed25519').is_file():
        print('keys/gw_ed25519 is missing; make-seed.sh creates it', file=sys.stderr)
        return 1
    vals = values()
    for vm in VMS:
        out = render(vm, vals)
        print(f'rendered {out}')
    has_token = 'env' in json.loads(settings_json())
    print('Claude Code token from keys/claude_oauth_token included in backend settings' if has_token
          else 'no keys/claude_oauth_token: Claude Code will ask to log in on the backend')
    print(f"git identity for the VM: {vals['GIT_USER_NAME']} <{vals['GIT_USER_EMAIL']}>")
    return 0


if __name__ == '__main__':
    sys.exit(main())
