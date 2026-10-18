#!/usr/bin/env python3
"""
Write the write_files entries of a rendered cloud-init user-data into a directory
tree, so the embedded scripts, profiles and rules can be linted as files.

  extract-files.py build/desk-seed/user-data build/extracted/desk

Each entry lands at <out>/<its path>, base64 entries decoded. Needs PyYAML; the
mise task runs it through `uv run --with pyyaml`.
"""
import base64
import pathlib
import sys

import yaml


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    user_data = yaml.safe_load(pathlib.Path(argv[1]).read_text())
    out = pathlib.Path(argv[2])
    count = 0
    for entry in user_data.get('write_files', []):
        target = out / entry['path'].lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        content = entry.get('content', '')
        if entry.get('encoding', '') in ('b64', 'base64'):
            target.write_bytes(base64.b64decode(content))
        else:
            target.write_text(content)
        count += 1
    print(f'{count} files from {argv[1]} under {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
