#!/bin/sh
# Finishes a fresh desk after its cloud-init (ide_sandboxing_summary.md 4.9; PLAN.md M1):
# waits for SSH with the rig key, pushes the wheel and the rig key pair, installs the package
# with `sudo pipx install --global`, runs `sudo sandboxed-devenv host install`, and reboots
# desk into GNOME. The same two commands provision the Linux laptop on its install day; the
# UTM driver takes this script over at M2.
#   ./provision-desk.sh            after `utmctl start desk`
# Environment: DESK_ADDRESS (default 192.168.64.10), SANDBOXED_DEVENV_HOME.
set -eu
cd "$(dirname "$0")"
root="$(cd ../.. && pwd)"
desk="${DESK_ADDRESS:-192.168.64.10}"
secrets="${SANDBOXED_DEVENV_HOME:-$HOME/.sandboxed_devenv}/secrets"
key="$secrets/gw_ed25519"
[ -f "$key" ] || { echo "no rig key at $key; run make-seed.sh first" >&2; exit 1; }
set -- "$root"/build/sandboxed_devenv-*.whl
wheel="$1"
[ -f "$wheel" ] || { echo "no wheel under $root/build/; run make-seed.sh (or sandboxed-devenv render) first" >&2; exit 1; }

# A fresh desk has fresh host keys: a known_hosts file of its own, reset here. M3 pins keys.
known_hosts="$(pwd)/build/desk.known_hosts"
rm -f "$known_hosts"
run_ssh() {
  ssh -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5 \
      -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$known_hosts" "jsw@$desk" "$@"
}
run_scp() {
  scp -q -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5 \
      -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$known_hosts" "$@"
}

started=$(date +%s)
echo "waiting for SSH on $desk; the first boot installs the desktop, which takes several minutes"
until run_ssh true 2>/dev/null; do
  sleep 10
done
echo "SSH up after $(( $(date +%s) - started )) s; waiting for cloud-init to finish"
status="$(run_ssh cloud-init status --wait || true)"
case "$status" in
  *done*) echo "cloud-init: $status" ;;
  *) echo "cloud-init did not finish cleanly: $status" >&2; exit 1 ;;
esac

echo "pushing $(basename "$wheel") and the rig key pair"
run_ssh 'mkdir -p ~/.sandboxed_devenv/secrets && chmod 700 ~/.sandboxed_devenv ~/.sandboxed_devenv/secrets'
run_scp "$wheel" "jsw@$desk:~/"
run_scp "$key" "$key.pub" "jsw@$desk:~/.sandboxed_devenv/secrets/"
run_ssh 'chmod 600 ~/.sandboxed_devenv/secrets/gw_ed25519'

echo "installing the package: sudo pipx install --global (pipx $(run_ssh pipx --version))"
run_ssh "sudo pipx install --global --force ~/$(basename "$wheel")"
run_ssh sandboxed-devenv --version

echo "provisioning: sudo sandboxed-devenv host install (downloads the Gateway launcher, minutes)"
run_ssh sudo sandboxed-devenv host install

echo "done after $(( $(date +%s) - started )) s; rebooting desk into GNOME"
echo "next: log in as jsw in the UTM window and run: sandboxed-devenv client start"
run_ssh sudo systemctl reboot || true
