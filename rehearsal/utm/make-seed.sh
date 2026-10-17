#!/bin/sh
# Builds the NoCloud seed ISOs for the two rehearsal VMs (ide_sandboxing_summary.md, 4.9).
#   ./make-seed.sh                 render the seed directories and build the ISOs
#   ./make-seed.sh --render-only   render only (what CI verifies; needs no ISO tool)
# Creates keys/gw_ed25519 once (the client sandbox's key), renders the user-data
# templates with render.py into build/<vm>-seed/, and writes build/<vm>-seed.iso.
# Environment overrides: GATEWAY_VERSION, IDEA_VERSION, DESK_PASSWORD,
# GIT_USER_NAME, GIT_USER_EMAIL (default: this machine's global git config).
# Optional input: keys/claude_oauth_token, a token from `claude setup-token` run
# on this Mac; when present it goes into the backend's Claude Code settings.
# macOS ships BSD mkdir, rm and ssh-keygen, which have no long options.
set -eu
cd "$(dirname "$0")"

render_only=0
if [ "${1:-}" = "--render-only" ]; then
  render_only=1
fi

# keys/ holds the client sandbox's key pair; build/ holds everything generated.
mkdir -p keys build

# Generate the gw key once: ed25519, no passphrase (-N ""), comment gw@desk (-C),
# written to keys/gw_ed25519 (-f). Reused on later runs.
[ -f keys/gw_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -C gw@desk -f keys/gw_ed25519

# Fill the templates: keys, versions, password, git identity, the canary and
# Claude Code's settings replace the @@NAME@@ placeholders.
python3 render.py

if [ "$render_only" -eq 1 ]; then
  echo "render only: no ISOs built"
  exit 0
fi

for vm in desk backend; do
  iso="build/$vm-seed.iso"
  rm -f "$iso"
  # An ISO 9660 image with Joliet names, volume label "cidata", which is what
  # cloud-init's NoCloud datasource looks for. hdiutil on macOS; xorrisofs or
  # genisoimage on Linux.
  if command -v hdiutil >/dev/null 2>&1; then
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o "$iso" "build/$vm-seed"
  elif command -v xorrisofs >/dev/null 2>&1; then
    xorrisofs -quiet -output "$iso" -volid cidata -joliet -rational-rock "build/$vm-seed"
  elif command -v genisoimage >/dev/null 2>&1; then
    genisoimage -quiet -output "$iso" -volid cidata -joliet -rock "build/$vm-seed"
  else
    echo "no ISO tool found (hdiutil, xorrisofs or genisoimage); use --render-only" >&2
    exit 1
  fi
  echo "built $iso"
done
echo "gw public key (already in backend's user-data): $(cat keys/gw_ed25519.pub)"
