#!/bin/sh
# Registers the rehearsal VMs in UTM from build/<vm>.raw and build/<vm>-seed.iso
# through UTM's AppleScript interface (ide_sandboxing_summary.md, 4.9).
#   ./create-vms.sh                both VMs
#   ./create-vms.sh desk           one of them, after `utmctl delete` of the old one
# Each VM: Apple Virtualization backend, 4 cores, 8 GB, UTM's defaults of one
# shared network and one serial console, plus a display for desk. Both the raw
# disk and the seed ISO are attached as non-removable drives, which UTM imports
# (copies) into the VM bundle. A removable drive would instead be referenced by
# path, and UTM, a sandboxed app, cannot read a path it was handed by script
# ("Cannot access resource" at first start, seen 8 October 2026). Copying also
# means make-seed.sh can rebuild the seed without disturbing an existing VM, and
# build/ stays pristine for the next fresh VM.
set -eu
cd "$(dirname "$0")"
here="$(pwd)"

vms="${*:-desk backend}"
for vm in $vms; do
  for f in "build/$vm.raw" "build/$vm-seed.iso"; do
    [ -f "$f" ] || { echo "missing $f: run make-seed.sh and the qemu-img steps first" >&2; exit 1; }
  done
done

# create <name> <memory MiB> <display yes|no>
create() {
  osascript - "$here" "$1" "$2" "$3" <<'APPLESCRIPT'
on run argv
  set here to item 1 of argv
  set vmName to item 2 of argv
  set memMiB to (item 3 of argv) as integer
  set wantDisplay to (item 4 of argv) is "yes"
  set diskFile to POSIX file (here & "/build/" & vmName & ".raw")
  set seedFile to POSIX file (here & "/build/" & vmName & "-seed.iso")
  tell application "UTM"
    if exists virtual machine named vmName then
      error "a VM named " & vmName & " already exists; remove it first with: utmctl delete " & vmName
    end if
    -- Drives with a source and no "removable:true" are imported (copied). The
    -- disk comes first so EFI boots from it; the seed has no boot loader and is
    -- found by cloud-init through its "cidata" filesystem label.
    set config to {name:vmName, memory:memMiB, cpu cores:4, drives:{{source:diskFile}, {source:seedFile}}}
    if wantDisplay then
      set config to config & {displays:{{dynamic resolution:true}}}
    end if
    make new virtual machine with properties {backend:apple, configuration:config}
  end tell
end run
APPLESCRIPT
}

# Keyboard and pointer are not reachable through the scripting interface and a
# scripted VM gets them "Disabled" (found 9 October 2026: GNOME came up with no
# input devices). Set them in the bundle's configuration on disk, then make UTM
# re-read it: newer UTM versions have a "reload configuration" command; 4.7.5
# does not, so the fallback quits and relaunches UTM, which re-reads every
# bundle. Only stopped VMs are affected either way; the fallback expects no VM
# to be running.
enable_input() {
  plist="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents/$1.utm/config.plist"
  [ -f "$plist" ] || { echo "no bundle for $1 at $plist" >&2; exit 1; }
  plutil -replace Virtualization.Keyboard -string Generic "$plist"
  plutil -replace Virtualization.Pointer -string Mouse "$plist"
  # Clipboard with the Mac; the guest side is spice-vdagent, installed by cloud-init.
  plutil -replace Virtualization.ClipboardSharing -bool true "$plist"
}
reread_bundles() {
  if osascript -e 'tell application "UTM"' -e "set vm to virtual machine named \"$1\"" -e 'reload configuration vm' -e 'end tell' >/dev/null 2>&1; then
    return
  fi
  echo "this UTM cannot reload one configuration; restarting UTM so it re-reads the bundles"
  osascript -e 'tell application "UTM" to quit'
  i=0; while pgrep -xq UTM && [ $i -lt 20 ]; do sleep 1; i=$((i+1)); done
  open -a UTM; sleep 4
}

for vm in $vms; do
  case "$vm" in
    desk) create desk 8192 yes; enable_input desk; reread_bundles desk ;;
    backend) create backend 8192 no ;;
    *) echo "unknown VM $vm (desk or backend)" >&2; exit 1 ;;
  esac
done
echo "registered; UTM now lists:"
utmctl list
