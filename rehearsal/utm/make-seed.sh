#!/bin/sh
# Builds the NoCloud seed ISOs for the two rehearsal VMs (ide_sandboxing_summary.md, 4.9).
#   ./make-seed.sh                 render the seed directories and build the ISOs
#   ./make-seed.sh --render-only   render only (what CI verifies; needs no ISO tool)
# The rig key pair lives in ~/.sandboxed_devenv/secrets/ (SANDBOXED_DEVENV_HOME moves the
# directory), created here once; a pair that earlier runs left in keys/ is moved there, with
# keys/claude_oauth_token if present. The desk seed comes from `sandboxed-devenv render`,
# which renders files/host/cloudinit__user-data.yaml.tmpl and builds the wheel under
# <checkout>/build/; render.py renders the backend seed (its template moves into the
# package at M2) and assembles build/desk-seed/ from the package's output.
# Environment overrides: GATEWAY_VERSION, IDEA_VERSION, DESK_PASSWORD, GIT_USER_NAME,
# GIT_USER_EMAIL (default: this machine's global git config).
# macOS ships BSD mkdir, rm and ssh-keygen, which have no long options.
set -eu
cd "$(dirname "$0")"
root="$(cd ../.. && pwd)"

render_only=0
if [ "${1:-}" = "--render-only" ]; then
  render_only=1
fi

# The rig key pair: ed25519, no passphrase (-N ""), comment gw@desk (-C). Made once.
secrets="${SANDBOXED_DEVENV_HOME:-$HOME/.sandboxed_devenv}/secrets"
mkdir -p "$secrets"
chmod 700 "$secrets"
if [ ! -f "$secrets/gw_ed25519" ]; then
  if [ -f keys/gw_ed25519 ]; then
    echo "moving the rig key pair from keys/ to $secrets"
    mv keys/gw_ed25519 keys/gw_ed25519.pub "$secrets/"
  else
    ssh-keygen -q -t ed25519 -N "" -C gw@desk -f "$secrets/gw_ed25519"
    echo "rig key pair created: $secrets/gw_ed25519"
  fi
fi
if [ -f keys/claude_oauth_token ] && [ ! -f "$secrets/claude_oauth_token" ]; then
  echo "moving keys/claude_oauth_token to $secrets"
  mv keys/claude_oauth_token "$secrets/"
fi
mkdir -p build

# The package renders the host role (the desk cloud-init among it) and builds the wheel that
# provision-desk.sh pushes. Inside the mise environment the project venv is on the PATH.
if ! command -v sandboxed-devenv >/dev/null 2>&1; then
  echo "sandboxed-devenv is not on the PATH: run \`mise run sync\` and retry inside the mise environment" >&2
  exit 1
fi
sandboxed-devenv render --out "$root/build"

# The backend seed from its template; the desk seed assembled from the package's output.
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
echo "rig public key (in both seeds): $(cat "$secrets/gw_ed25519.pub")"
