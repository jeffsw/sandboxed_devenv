# Plan: from rehearsal scripts to a Python package

Status: proposal, 23 October 2026. The design itself is in `ide_sandboxing_summary.md`;
this file is the implementation plan for turning the rehearsal rig into an installable
tool that runs the same way on the Mac (UTM) and on the Framework laptop (Incus).

## Goal

One Python package, `sandboxed_devenv`, with one command (working name `sdev`, see
"Open decisions") that manages three things:

1. **The host**: the client sandbox (`gw` user, its scripts, the AppArmor profile, the
   egress rules) and the VM platform. On the Framework the host is the laptop; on the Mac
   it is the `desk` VM, which is what keeps the Mac useful as a test bed.
2. **Backend VMs**: a base image built once from the backend cloud-init, and per-project
   instances launched from it in seconds, each with its own credentials, plus disposable
   clones.
3. **Provisioning and verification**: rendering every artifact without side effects (for
   CI), building and checking VMs headlessly (for the Mac and later the Framework), and
   the canary as the assertion library.

Platform differences live in one driver layer. Everything above it is shared.

## Principles

- **Mechanism below, policy above.** Drivers know how to create a VM, attach cloud-init
  and find its address. They do not know what a backend is.
- **Idempotent and inspectable.** Every mutating command has `--dry-run`, prints the
  commands it would run, and can be re-run safely.
- **Secrets never enter rendered artifacts in CI.** Locally they come from a secrets
  directory or a secrets manager at render time; CI renders with placeholders. This is
  what `render.py` already does and it stays.
- **The canary stays a single stdlib file.** It is copied into every VM and must run
  there with nothing installed.
- **Same conventions as rpkilog**: setuptools, `argparse`, `pydantic` for models, `pytest`
  with importlib mode, tools pinned in `.mise.toml`, checks defined once as mise tasks and
  run by CI.

## Target layout

```
pyproject.toml                   package metadata, entry point sdev = sandboxed_devenv.cli:main
sandboxed_devenv/
  cli.py                         argparse subcommands, thin: parses and calls the modules below
  config.py                      user config + versions + policy models (pydantic), tomllib readers
  render.py                      template rendering (from rehearsal/utm/render.py), no side effects
  canary.py                      the canary, unchanged in spirit: stdlib only, runnable standalone
  host/
    install.py                   host provisioning: users, files, AppArmor, nftables, Gateway, linger
    files/                       the files that used to be base64 blobs in the desk template,
      usr/local/bin/gw-run         stored at their target path, modes in a manifest
      usr/local/bin/gw-session
      usr/local/bin/gw-exec
      etc/apparmor.d/jetbrains-client
      etc/nftables.conf.tmpl
      etc/sysctl.d/99-vmrouter.conf
      home/gw/.ssh/config.tmpl
      home/gw/.config/JetBrains/...sshConfigs.xml.tmpl
  backend/
    base.yaml.tmpl               the base image cloud-init: packages, IDE backend, plugin, tools
    instance.yaml.tmpl           the per-instance cloud-init: hostname, keys, identity, token, clone
    network-config.tmpl
  drivers/
    base.py                      the Driver protocol
    utm.py                       utmctl, osascript, plutil, hdiutil (from create-vms.sh, make-seed.sh)
    incus.py                     incus CLI with --format json
  inventory.py                   instances and their credentials, one JSON file per instance
  creds.py                       mint, register, rotate, revoke (gh deploy keys, Claude token, gw key)
  connect.py                     Gateway sshConfigs.xml, gw's ssh_config and known_hosts
  testing.py                     the headless provisioning harness
tests/                           pytest: renderer golden files, drivers against recorded CLI output,
                                 inventory, policy to nftables, canary parsing
rehearsal/
  utm/desk/                      the desk VM's cloud-init: install the package wheel, run sdev host install
  ci/                            puppeteer config for the Mermaid check (unchanged)
docs/
  runbook.md                     install, new project, dispose, rotate, troubleshoot
  framework-install.md           the laptop's first day, step by step
policy.toml                      egress allowlists and the VM subnet
versions.toml                    Gateway, IDE backend, client, Ubuntu image serial, tool versions
```

`rehearsal/utm/{make-seed.sh,create-vms.sh,extract-files.py,render.py}` are replaced by the
package and deleted once M2 reaches parity. `ide_sandboxing_summary.md` stays the design
record; its 4.9 commands get replaced by `sdev` equivalents at M2.

## CLI surface

| Command | Does |
|---|---|
| `sdev doctor` | Reports which driver is usable (utmctl, incus), tool versions, config and secrets dirs |
| `sdev render [--out DIR]` | Renders every artifact (cloud-init, nftables, AppArmor, ssh configs) without touching a VM. CI's entry point |
| `sdev host install [--dry-run]` | Provisions the host: `gw` user and linger, files from the manifest, AppArmor, sysctl, nftables, Gateway tarball. Linux only, needs root |
| `sdev host check` | Runs the canary in the client role via `gw-exec` and prints the summary |
| `sdev image build [--force]` | Downloads the Ubuntu cloud image, boots it with `base.yaml`, waits, runs `cloud-init clean`, powers off, registers the base image |
| `sdev image ls / rm` | Base images and their versions |
| `sdev new NAME --repo OWNER/REPO [--disposable] [--write]` | Launches an instance from the base image with its own cloud-init, waits for cloud-init, mints and registers the deploy key, pins the host key, writes the Gateway connection, records everything in the inventory |
| `sdev ls` | Instances, state, address, repository, credential ages |
| `sdev start / stop / rm NAME` | Lifecycle; `rm` revokes every credential the inventory lists, then deletes the VM |
| `sdev clone NAME NEW` | Disposable copy; the clone gets fresh instance credentials |
| `sdev ssh NAME [CMD]`, `sdev console NAME` | Shell as `dev` with the rig key; serial console via the driver |
| `sdev creds ls [NAME]` | Every credential, scope, age, where it lives, how it is revoked |
| `sdev creds rotate NAME [--deploy-key] [--claude-token]` | Regenerate inside the VM, re-register, update the inventory |
| `sdev canary --role client\|backend` | The canary itself, so one file serves both the package and the VMs |
| `sdev test provision` | The headless harness: image, instance, canary both roles, SSH path, teardown |

## Work items

Checkboxes are implementation steps. Items already true today are checked so the list
shows where the rig stands.

### M0: package scaffold

- [ ] Decide the command name (see "Open decisions"); `sdev` is used below.
- [ ] `pyproject.toml`: setuptools, `requires-python >= 3.12`, dependencies `pyyaml` and
      `pydantic`, dev group `pytest`, `ruff`; entry point `sdev`; package data for
      `host/files/**` and `backend/*.tmpl`.
- [ ] `sandboxed_devenv/cli.py` with `argparse` subparsers, one function per subcommand,
      `--dry-run` and `--verbose` as global flags, exit codes: 0 ok, 1 failure, 2 usage.
- [ ] `uv.lock`; `.mise.toml` gains `_.python.venv = ".venv"` and a `uv sync` task like
      rpkilog's; `lint-python` and a new `test` task run inside the venv.
- [ ] `tests/conftest.py` and a first test that `sdev --help` lists every subcommand.
- [ ] CI: `mise run verify` also runs `pytest`; the workflow installs the package in the
      venv. The macOS job stays manual.
- [ ] `AGENTS.md` with the verification contract, as in rpkilog; `CLAUDE.md` includes it.
- [x] Tools pinned in `.mise.toml`; checks defined once as mise tasks; CI runs `mise run verify`.

### M1: host provisioning extraction

The first payoff on both machines: real files instead of base64 blobs, lintable in place,
and one installer that runs on the Framework and inside `desk`.

- [ ] Move every `write_files` entry of `rehearsal/utm/desk/user-data.tmpl` to
      `sandboxed_devenv/host/files/<target path>`; a `manifest.toml` lists owner, group,
      mode and whether the file is a template.
- [ ] `host/install.py`: idempotent steps, each reporting changed/unchanged: create `gw`
      (no sudo, linger on), copy files from the manifest, `apparmor_parser --replace`,
      `sysctl --system`, nftables (installed, not enabled until policy says so), unpack the
      Gateway tarball for the pinned version and add the toolkit line to its vmoptions,
      write `gw`'s key pair and `ssh_config` from the secrets dir.
- [ ] `sdev host install --dry-run` prints the plan; `--check` exits non-zero if anything
      would change (for the headless harness and for drift checks on the laptop).
- [ ] Delete `rehearsal/utm/extract-files.py` and the `extract` task; `lint-shell`,
      `lint-apparmor` and `lint-nft` read `host/files/` directly, with templates rendered
      by `sdev render`.
- [ ] The desk cloud-init shrinks to: packages, the `jsw` user, the rig key, the package
      wheel in one `write_files` entry, `runcmd: pip install` the wheel and `sdev host
      install`. The wheel is built by `sdev render` (via `python -m build`).
- [ ] Rebuild `desk` from the new template; `sdev host check` passes with 0 FAIL;
      Gateway starts via `gw-run`; Markdown preview and clipboard still work.
- [x] The host pieces exist and are proven on `desk`: `gw-run`, `gw-session`, `gw-exec`,
      the AppArmor profiles, the runtime-dir ACLs, the private Xwayland with a cookie.

### M2: drivers, rendering and the backend split

Parity with today's rig, then the old scripts go.

- [ ] `drivers/base.py`: a `Driver` protocol with `create(name, image, cpus, memory,
      seed)`, `start`, `stop`, `delete`, `clone`, `list`, `address(name)`,
      `console(name)`, `wait_ready(name)` (polls `cloud-init status` over SSH with the
      rig key), and `deliver_seed(name, dir)` because UTM wants an ISO attached as a
      drive and Incus wants config keys.
- [ ] `drivers/utm.py`: port `create-vms.sh` (AppleScript create, plutil for keyboard,
      pointer and clipboard, the UTM relaunch) and `make-seed.sh` (hdiutil ISO). Keep the
      documented UTM 4.7.5 quirks as comments with dates.
- [ ] `versions.toml`: Gateway, IDE backend, client build, Ubuntu release and image
      serial, architecture-specific download URLs keyed by `aarch64` and `x86_64`.
      `render.py` reads it; nothing else carries a version string.
- [ ] Split `rehearsal/utm/backend/user-data.tmpl` into `backend/base.yaml.tmpl` (packages,
      `gh` apt source, IDE tarball and `registerBackendLocationForGateway`, plugin
      pre-install, tools, `mise` and `direnv` hooks, sshd hardening, canary) and
      `backend/instance.yaml.tmpl` (hostname, `dev`'s authorized key, deploy key
      generation, GitHub host keys, git identity, Claude settings, optional project
      clone, deploy key printed to the console).
- [ ] `sdev image build`: boot the cloud image with the base cloud-init, wait, `cloud-init
      clean --logs`, power off; UTM keeps the raw disk under `build/images/` named by
      version, Incus publishes an image alias. Rebuilding is forced by a version change
      or `--force`.
- [ ] `sdev new`, `ls`, `start`, `stop`, `rm`, `clone`, `ssh`, `console` working on the Mac
      with the UTM driver; the per-instance seed rendered from `instance.yaml.tmpl`.
- [ ] `policy.toml`: client egress (VM subnet, JetBrains download and account hosts),
      backend egress (GitHub, package indexes, Anthropic), the VM subnet. `sdev render`
      produces the host nftables ruleset from it; on the Mac the same ruleset lands on
      `desk` for the client half only, since UTM's private shared network cannot route
      the VMs through `desk` (known gap, Framework only).
- [ ] Replace the 4.9 command blocks in the summary with `sdev` commands; delete
      `make-seed.sh`, `create-vms.sh`, `render.py` under `rehearsal/utm/`; the mise
      `render` and `schema` tasks call `sdev render`.
- [x] Templates, `render.py` with placeholder checking, `cloud-init schema` in CI, seed
      ISOs, VM creation via AppleScript, inter-VM host routes: all working on the Mac.

### M3: credentials and connections

- [ ] `inventory.py`: one JSON file per instance under the state dir with name,
      driver, repository, image version, created, and a `credentials` list; each entry
      has kind, scope, fingerprint or id, where it lives, created, and the revoke action.
- [ ] `creds.py`: deploy key (fetch the public key over SSH, `gh repo deploy-key add`,
      record the key id; `revoke` runs `gh repo deploy-key delete`), Claude token (from
      the secrets dir, hashed fingerprint only in the inventory; no revoke API, so
      `creds ls` shows age and the runbook says how to rotate at the account), `gw`'s
      key pair (minted once per host, recorded with its fingerprint), git identity.
- [ ] `sdev rm` revokes everything listed before deleting the VM; `sdev clone` gives the
      clone fresh instance credentials rather than copying the parent's.
- [ ] `connect.py`: write the Gateway `sshConfigs.xml` entry on a live host (only when
      Gateway is not running, else print instructions), add the `Host` stanza to `gw`'s
      `ssh_config`, pin the VM's SSH host key in `gw`'s `known_hosts` from the public key
      read through the driver rather than by trusting first use.
- [ ] Secrets dir: `~/.config/sandboxed-devenv/secrets/` (mode 0700) holding the rig key
      pair and `claude_oauth_token`; optional `op://` references resolved with the
      1Password CLI at render time. `sdev doctor` reports which is in use.
- [ ] Policy knob in the user config: one Claude token for all VMs or one per trust
      level; default one token, as the open question in the summary suggests.
- [x] The deploy key per VM, `IdentityAgent none`, the Claude token in `settings.json`,
      and the pre-provisioned Gateway connection are proven on the rig; registration is
      still manual (summary 4.9, "manual steps").

### M4: testing

- [ ] `canary.py` gains `--json` output (one object per probe plus a summary) and keeps
      the human output; exit code non-zero on any FAIL (already) and a `--expect FILE`
      mode that compares against a golden result for the role.
- [ ] Unit tests: renderer golden files for both VMs and both architectures; drivers
      against recorded `utmctl list` and `incus list --format json` output; inventory
      round-trips; `policy.toml` to nftables checked with `nft --check` on Linux.
- [ ] `sdev test provision`: builds or reuses the image, `sdev new ci-NAME`, waits, runs
      the canary in both roles, checks SSH from `gw` to the instance and that the backend
      service is present, then `sdev rm`. Runs on the Mac on demand (`mise run
      test-provision`) and later on the Framework; never in GitHub CI.
- [ ] The interactive checklist in `docs/runbook.md`: Gateway, client, Wayland toolkit in
      About, Markdown preview, clipboard both ways, Claude Code `/status`, git fetch
      without a key dialog, plugin install path. This is the part that stays manual.
- [ ] `lint-doc` house rules extended to `PLAN.md`, `README.md` and `docs/`.
- [x] Static verification in CI: schema, AppArmor and nftables parsers, shell, Python,
      YAML, workflow, doc rules and Mermaid rendering.

### M5: the Framework

Written before the laptop arrives, first run on it.

- [ ] `drivers/incus.py`: `incus launch images:ubuntu/<release>/cloud NAME --vm` with
      `cloud-init.user-data` and `cloud-init.network-config` config keys, `incus publish`
      for the base image, `incus copy` for clones, `incus list --format json` for state
      and addresses, `incus console` for the serial console.
- [ ] Host nftables from `policy.toml` covering both halves: the `gw` uid for the client
      and the forward chain on the Incus bridge for the VMs. Incus network ACLs are the
      alternative if the bridge rules fight with Incus's own firewall management; decide
      on the laptop.
- [ ] `docs/framework-install.md`: Ubuntu 26.04 per Framework's guide, full-disk
      encryption, a separate volume for the Incus storage pool, the operations user or VM
      for router access (summary section 5), `uv tool install` of the package from the
      repository, `sudo sdev host install`, `sdev image build`, `sdev new` for the first
      project, `sdev host check`.
- [ ] Run `sdev test provision` on the laptop; record timings in the summary (step 6 of
      4.9 asked for them).
- [ ] The Framework's AppArmor and userns state is the same Ubuntu 26.04 default as
      `desk`; verify the profiles load and the client's embedded browser works before
      declaring the host done.

### M6: later

- [ ] Snapshots before disposal (`sdev snapshot NAME`), so uncommitted work in a project
      VM is recoverable for a while.
- [ ] Version bump workflow: change `versions.toml`, `sdev image build`, `sdev ls` shows
      which instances run an older image; `sdev upgrade NAME` recreates from the new image
      with the same credentials policy.
- [ ] Cloud and service CLIs per project (`aws`, Linode, Terraform) and their scoped
      credentials, the open question in the summary.
- [ ] Section 5 hardening on the host: the 1Password agent allowlist, nothing router-related
      in the development principal.
- [ ] The agent-level sandbox inside backend VMs (summary, "Deferred work"), once the rest
      is routine.

## Order and gates

| Milestone | Gate to pass | Where |
|---|---|---|
| M0 scaffold | `mise run verify` green with the package installed and one test | CI |
| M1 host extraction | `desk` rebuilt from the slim template; `sdev host check` 0 FAIL; preview and clipboard work | Mac |
| M2 drivers and backend split | `sdev new` to a connected client on the Mac; old scripts deleted | Mac |
| M3 credentials and connections | `sdev new` and `sdev rm` need no manual GitHub or Gateway step | Mac |
| M4 testing | `mise run test-provision` passes on the Mac unattended | Mac |
| M5 Framework | `sdev test provision` passes on the laptop; first real project opened | Framework |

M1 through M4 run on the Mac rig as it exists today, two UTM VMs, no nested
virtualization. The Mac remains the place to test changes after the Framework is the daily
machine, which is the reason the UTM driver is kept rather than retired.

## Open decisions

- [ ] **Command name.** `devenv` is taken by devenv.sh (Nix) and `devbox` by Jetify.
      `sdev` is the working name in this plan, but PyPI already has an active `sdev`
      (a serial-console toolkit, released June 2026) that installs a command of that
      name, so the real command should differ. Free on PyPI and Homebrew as of 23 October:
      `sdenv`, `sandev`, `sbdev`. The distribution name `sandboxed-devenv` is free.
      Decide before M0.
- [ ] **State dir.** `~/.local/state/sandboxed-devenv/` for the inventory and
      `~/.config/sandboxed-devenv/` for config and secrets, or everything under one
      directory. Default proposed: the two XDG locations.
- [ ] **Base image on UTM.** A raw disk copied per instance (simple, what the rig does
      today) versus `utmctl clone` of a stopped base VM (faster, but UTM copies the whole
      bundle). Proposed: raw disk copy, since UTM's clone is not the Framework's path.
- [ ] **Package install on the host.** `uv tool install git+ssh://...` from the private
      repository versus a wheel built locally and installed with `pipx`. Proposed: `uv tool`
      from the repository on the Framework; the wheel only inside `desk`.
- [ ] **Incus storage.** Directory, btrfs or ZFS pool on the Framework; affects the
      partitioning on install day. Proposed: btrfs on its own volume.
- [ ] **Host distribution.** Ubuntu 26.04 is assumed throughout (AppArmor profiles,
      Framework's certification). Changing it reopens M1.
