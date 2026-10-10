#!/bin/sh
# Builds the NoCloud seed ISOs for the two rehearsal VMs (ide_sandboxing_summary.md, 4.9).
#   ./make-seed.sh
# Creates keys/gw_ed25519 once (the client sandbox's key), renders the user-data
# templates, and writes build/desk-seed.iso and build/backend-seed.iso.
# Environment overrides: GATEWAY_VERSION, IDEA_VERSION, DESK_PASSWORD,
# GIT_USER_NAME, GIT_USER_EMAIL (default: this Mac's global git config).
# Optional input: keys/claude_oauth_token, a token from `claude setup-token` run
# on this Mac; when present it goes into the backend's Claude Code settings.
# macOS ships BSD mkdir, rm and ssh-keygen, which have no long options.
set -eu
cd "$(dirname "$0")"
export GATEWAY_VERSION="${GATEWAY_VERSION:-2026.2.2}"
export IDEA_VERSION="${IDEA_VERSION:-2026.2.3}"
export DESK_PASSWORD="${DESK_PASSWORD:-rehearsal}"
# Gateway keeps its settings under ~/.config/JetBrains/JetBrainsGateway<major.minor>/.
export GATEWAY_BRANCH="$(printf '%s' "$GATEWAY_VERSION" | cut -d. -f1,2)"
export GIT_USER_NAME="${GIT_USER_NAME:-$(git config --global user.name || true)}"
export GIT_USER_EMAIL="${GIT_USER_EMAIL:-$(git config --global user.email || true)}"

# keys/ holds the client sandbox's key pair; build/ holds everything generated.
mkdir -p keys build

# Generate the gw key once: ed25519, no passphrase (-N ""), comment gw@desk (-C),
# written to keys/gw_ed25519 (-f). Reused on later runs.
[ -f keys/gw_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -C gw@desk -f keys/gw_ed25519

for vm in desk backend; do
  out="build/$vm-seed"
  rm -rf "$out" "build/$vm-seed.iso"
  mkdir -p "$out"

  # Render the template: public key, base64 private key, versions, password, git
  # identity, the canary script (rehearsal/canary.py, base64) and Claude Code's
  # settings (with the token if keys/claude_oauth_token exists) replace the
  # @@NAME@@ placeholders.
  python3 - "$vm/user-data.tmpl" "$out/user-data" <<'PY'
import base64, json, os, sys
src, dst = sys.argv[1], sys.argv[2]
settings = {"theme": "dark"}
if os.path.exists("keys/claude_oauth_token"):
    token = open("keys/claude_oauth_token").read().strip()
    if token:
        settings["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": token}
vals = {
    "GIT_USER_NAME": os.environ["GIT_USER_NAME"],
    "GIT_USER_EMAIL": os.environ["GIT_USER_EMAIL"],
    "CLAUDE_SETTINGS_B64": base64.b64encode((json.dumps(settings, indent=2) + "\n").encode()).decode(),
    "GW_PUBKEY": open("keys/gw_ed25519.pub").read().strip(),
    "GW_PRIVKEY_B64": base64.b64encode(open("keys/gw_ed25519", "rb").read()).decode(),
    "GATEWAY_VERSION": os.environ["GATEWAY_VERSION"],
    "GATEWAY_BRANCH": os.environ["GATEWAY_BRANCH"],
    "CANARY_B64": base64.b64encode(open("../canary.py", "rb").read()).decode(),
    "IDEA_VERSION": os.environ["IDEA_VERSION"],
    "DESK_PASSWORD": os.environ["DESK_PASSWORD"],
}
text = open(src).read()
for k, v in vals.items():
    text = text.replace("@@%s@@" % k, v)
assert "@@" not in text, "unfilled placeholder in " + src
open(dst, "w").write(text)
PY
  cp "$vm/meta-data" "$vm/network-config" "$out/"

  # An ISO 9660 image with Joliet names, volume label "cidata", which is what
  # cloud-init's NoCloud datasource looks for.
  hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o "build/$vm-seed.iso" "$out"
  echo "built build/$vm-seed.iso"
done
echo "gw public key (already in backend's user-data): $(cat keys/gw_ed25519.pub)"
[ -s keys/claude_oauth_token ] && echo "Claude Code token from keys/claude_oauth_token included in backend's settings" || echo "no keys/claude_oauth_token: Claude Code will ask to log in on the backend"
