# Plan: from rehearsal scripts to a Python package

Status: proposal, 23 October 2026, revised 1 November so that one package, installed on
every machine involved, carries every entry point. The design itself is in
`ide_sandboxing_summary.md`; this file is the implementation plan for turning the
rehearsal rig into an installable tool that runs the same way on the Mac (UTM) and on the
Linux laptop (Incus).

## Goal

Keep the developer's laptop, and everything on it, out of reach of the code it works on.
A repository may pull in a compromised dependency, or may itself be the attack:
recruiting scams hand developers "take-home" repositories that are malware. Every project
opens in its own virtual machine with only the materials that project needs: its
checkouts, one deploy key per repository and one model token. The laptop itself and every
other project stay out of the VM's reach, so a compromise is confined to one project's
materials and ends when the VM is deleted.

One Python package, `sandboxed_devenv`, with one command, `sandboxed-devenv`, installed on
every machine that takes part (the Roles table below), gives the developer:

- **A sandboxed IDE client** on the laptop: the thin client as a second user with the
  display socket and nothing else.
- **One backend VM per project**, launched in seconds from a base image, holding the
  project's repositories, and deleted as freely as it was made.
- **Credential management**: a deploy key per repository, minted inside the VM,
  registered and revoked from the laptop as repositories are added and removed, rotated
  on demand.
- **An inventory** of every backend and every credential with its GitHub id and state, so
  nothing is orphaned when a backend goes, and `creds audit` can prove it.
- **Reproducible provisioning**: every artifact rendered without side effects for CI, VMs
  built and checked headlessly, and the canary as the assertion that the sandbox holds.

Platform differences live in one driver layer, UTM on the Mac and Incus on the Linux
laptop. Everything above it is shared, and `sandboxed-devenv --help` is the starting point
on every machine.

## Roles

Each machine plays one or more roles and runs the subcommands for those roles. The
control side reaches the other two by running the same command over SSH, so no files are
copied around and no remote configuration is edited by hand.

| Role | On the Mac rig | On the Linux laptop | Subcommands |
|---|---|---|---|
| control | the Mac | the laptop | `image`, `client-host`, `new`, `ls`, `start`, `stop`, `rm`, `clone`, `resources`, `repo`, `ssh`, `serial-console`, `creds`, `test` |
| client host | the `desk` VM | the laptop | `host`, `client`, `connect` |
| backend | each backend VM under UTM | each backend VM under Incus | `backend` |
| any | every machine above | every machine above | `doctor`, `render`, `canary`, `--version` |

The user config names the client host: `client_host: local` on the Linux laptop,
`client_host: ssh://jsw@192.168.64.10` on the Mac. `doctor` reports which roles the
machine it runs on can play, and a subcommand run in the wrong place says so and exits 1.

## Projects and repositories

A **project** is the unit of work: a directory `~/proj/<project>` holding one or more
repositories, the layout used on the Mac. A **backend** is the VM that serves one project,
and there is exactly one per project, so every command that needs a backend takes the
project's name as `PROJECT` and finds the backend through the inventory; no command takes
a backend name. The VM itself is named after the project with a prefix, `vm_name_prefix`
in `~/.sandboxed_devenv/config.yaml`, default `idesbx-`, so `utmctl list` and `incus list`
show `idesbx-ide_sandbox` apart from other VMs on a shared hypervisor. Inside the VM the
repositories are checked out at `~/proj/PROJECT/<repo>`, the same layout as outside, and
the Gateway connection opens `~/proj/PROJECT`, where the IntelliJ project spans them.
Repositories come and go over the life of the backend: `new` takes none or several,
`repo add` and `repo rm` change the set later. A disposable backend is a project too, a
short-lived one with a name to match.

Each repository gets its own deploy key pair inside the VM, because GitHub refuses a
deploy key that is already registered on another repository. The pair lives at
`~/.ssh/deploy_<owner>_<repo>`, an alias stanza in `dev`'s `ssh_config`
(`Host github.com-<owner>-<repo>`, `IdentitiesOnly yes`) binds it to that repository,
and the checkout's remote uses the alias. Adding a repository mints and registers a key;
removing one revokes it on GitHub first, then deletes it in the VM, and leaves the
checkout unless asked otherwise. The inventory records every key with its GitHub id, so
`rm` of the project revokes them all, and a revocation that fails is kept as
`pending-revoke` until `creds audit` gets it through.

## Principles

- **One package, installed wherever it runs.** No scripts beside it: the `gw` launcher
  helpers, the canary and the backend's first-boot steps are all subcommands. The only
  things copied as files are configuration: the AppArmor profile, nftables, sysctl, ssh
  configs.
- **Mechanism below, policy above.** Drivers know how to create a VM, attach cloud-init
  and find its address. They do not know what a backend is.
- **Idempotent and inspectable.** Every mutating command has `--dry-run`, prints the
  commands it would run, and can be re-run safely. A step expected to take more than a
  few seconds says so before it starts, says why, and reports the time it took; the
  hypervisor's own progress output is passed through where it exists, and the tool
  draws no progress bars of its own.
- **Secrets never enter rendered artifacts in CI.** Locally they come from a secrets
  directory or a secrets manager at render time; CI renders with placeholders. This is
  what `render.py` already does and it stays.
- **The canary is a stdlib-only module.** It imports nothing outside the standard
  library, so `python3 -m sandboxed_devenv.canary` still works on a VM whose install is
  half done. It is no longer copied anywhere as a file.
- **Same conventions as rpkilog**: setuptools, `argparse`, `pydantic` for models, `pytest`
  with importlib mode, tools pinned in `.mise.toml`, checks defined once as mise tasks and
  run by CI.

## Target layout

Paths are relative to the repository root, `~/proj/ide_sandbox/sandboxed_devenv/`. The
Python package directory inside it has the same name, as rpkilog's does
(`python/rpkilog/rpkilog/`); `pyproject.toml` sits at the repository root beside it. The
package is flat, like rpkilog's: one directory of modules, prefixed by the role they
serve, with two data directories beside them and no subpackages.

```
pyproject.toml                   metadata, entry point sandboxed-devenv = sandboxed_devenv.cli:main
sandboxed_devenv/
  cli.py                         one argparse tree; each role module registers its subcommands
  common.py                      the package version, subcommand groups, the not-yet-implemented stub
  doctor.py                      doctor: roles this machine can play, drivers found, ~/.sandboxed_devenv
  config.py                      user config + versions + policy models (pydantic), YAML readers
  render.py                      template rendering and the wheel build, no side effects
  steps.py                       idempotent steps shared by host install and backend prepare: outcomes, atomic file sync
  canary.py                      stdlib-only probes; the `canary` subcommand; runnable with python3 -m
  remote.py                      runs the command on the client host or a backend over SSH, parses --json
  control_driver.py              the Driver protocol
  control_driver_utm.py          utmctl, osascript, plutil, hdiutil (from create-vms.sh, make-seed.sh)
  control_driver_incus.py        incus CLI with --format json
  control_images.py              image build / ls / rm
  control_backends.py            new, ls, start, stop, rm, clone, resources, ssh, serial-console: one backend per project
  control_client_host.py         client-host new / rm / ssh: the desk VM on the Mac rig (the Linux laptop is its own client host)
  control_inventory.py           backends and their credentials, one JSON file per project
  control_creds.py               mint, register, rotate, revoke (gh deploy keys, Claude token, gw key)
  control_testing.py             the headless provisioning harness
  host_install.py                host install / check: users, files, AppArmor, nftables, Gateway, linger
  host_client.py                 client start / session / exec (what gw-run, gw-session, gw-exec do)
  host_connect.py                connect add / rm / ls: Gateway sshConfigs.xml, gw's ssh_config, known_hosts
  backend_prepare.py             backend prepare / status / repo
  files/                         everything a role receives on disk, plus cloud-init; package data
    manifest.yaml                dest, owner, group, mode, template, stage, description per file
    host/
      cloudinit__user-data.yaml.tmpl            the desk VM on the Mac rig: user, rig key; `client-host new` does the rest
      cloudinit__network-config.yaml.tmpl       desk's static address and the routes to both halves of the VM subnet
      etc__apparmor.d__jetbrains-client.apparmor
      etc__nftables.conf.tmpl
      etc__sysctl.d__99-vmrouter.conf
      home__gw__.ssh__config.ssh_config         includes config.d/, where `connect add` writes one stanza per project
      home__gw__.config__JetBrains__JetBrainsGateway@@GATEWAY_BRANCH@@__options__sshConfigs.xml.tmpl   empty; `connect add` fills it
    backend/
      cloudinit-image__user-data.yaml.tmpl      the base image: packages, IDE backend, plugin, tools
      cloudinit-project__user-data.yaml.tmpl    a project's backend: hostname, keys, project config, prepare
      cloudinit__network-config.yaml.tmpl
      etc__ssh__sshd_config.d__10-backend.conf
      home__dev__.ssh__config.ssh_config
      home__dev__.claude__settings.json.tmpl
  versions.yaml                  Gateway, IDE backend, client, Ubuntu image serial; package data, so every install carries its pins
  policy.yaml                    egress allowlists and the VM subnet; package data too, since `host install` on a VM reads it;
                                 ~/.sandboxed_devenv/policy.yaml overrides it (the laptop's uplink interface)
tests/                           pytest, one test module per package module, as in rpkilog
ci/                              puppeteer config for the Mermaid check (moved from rehearsal/ci/ at M2)
docs/
  runbook.md                     install, new project, dispose, rotate, troubleshoot
  linux-laptop-install.md        the laptop's first day, step by step
```

`rehearsal/` went away at M2 (8 November): its scripts were replaced by the package (the
UTM driver, `image build`, `client-host new`, `new`), the desk template became
`files/host/cloudinit__user-data.yaml.tmpl`, and `rehearsal/ci/` moved to `ci/`.
`ide_sandboxing_summary.md` stays the design record; its 4.9 commands are the
`sandboxed-devenv` equivalents since revision 12.

### Files and templates

Everything a role receives on disk, and every cloud-init document, lives under
`files/<role>/`, named so that the listing above answers where a file goes, what format
it is and whether it is rendered:

- **Name is the destination.** Leading slash dropped, other slashes become `__`, dots
  kept. Cloud-init documents have no destination, so they take `cloudinit__` plus the
  NoCloud file name, with `cloudinit-image__` and `cloudinit-project__` for the backend's
  two.
- **Extension is the format.** The destination's own, or an added `.apparmor`,
  `.ssh_config` or `.yaml`. A trailing `.tmpl` marks rendered content; anything else is
  copied verbatim.
- **Variable path segments** appear as `@@NAME@@`, as inside templates, so the filename
  decodes to the manifest's `dest`. The Gateway branch is the one case today.
- **`manifest.yaml`** holds what a file cannot say about itself: `dest`, `owner`,
  `group`, `mode` (quoted, since YAML reads `0644` as octal), `template`, `description`,
  for backend files the `stage`, `image` or `project`, and `keep` for a file written once
  and then left to the command that manages it (Gateway's `sshConfigs.xml`, which
  `connect add` fills).
- **Each file opens with a header** naming its role and destination, such as
  `# sandboxed-devenv host: rendered to /etc/nftables.conf`; on line two of cloud-init
  documents, where `#cloud-config` comes first, and absent from JSON.
- **`tests/test_files_manifest.py`** checks that the manifest, the tree, the filenames
  and the headers agree.

## CLI surface

| Command | Role | Does |
|---|---|---|
| `sandboxed-devenv --version` | any | The package version; it must agree on every machine or `test end-to-end` fails |
| `sandboxed-devenv doctor` | any | Which roles this machine can play, driver and tool versions, what `~/.sandboxed_devenv/` holds, the version on each client host and backend it can reach |
| `sandboxed-devenv render [--out DIR]` | any | Renders every artifact (cloud-inits, nftables, AppArmor, ssh configs) and builds the wheel, without touching a VM. CI's entry point |
| `sandboxed-devenv canary --role ROLE [--json]` | any | The probes for the client or backend role, run locally |
| `sandboxed-devenv image build [--force] [--wheel PATH] [--cpus N] [--memory-gib N]` | control | Downloads the Ubuntu cloud image (checked against its SHA256SUMS), boots it once with the base cloud-init, waits for SSH and cloud-init, pushes and installs the wheel (built from the checkout unless `--wheel` names one), runs `backend prepare --stage image`, `cloud-init clean`, powers off, keeps the disk as the base image with a record of what went in. Skips the build when nothing changed; `--force` rebuilds |
| `sandboxed-devenv image ls / rm` | control | Base images and their versions |
| `sandboxed-devenv client-host new [--replace] [--wheel PATH] [--cpus N] [--memory-gib N]` | control | The desk VM on the Mac rig: the cloud image as a raw disk, the host cloud-init and network-config (static address, routes to both halves of the VM subnet), a VM with a display, then over SSH the wheel and `sudo pipx install --global`, the rig key pair into the host user's secrets directory, `sudo sandboxed-devenv host install`, a reboot into GNOME. Refuses when `client_host` is `local`: the laptop is its own client host |
| `sandboxed-devenv client-host provision [--wheel PATH]` | control | The SSH half of `client-host new` on an existing desk: the wheel and `sudo pipx install --global`, the sandbox key pair into the host user's secrets directory, `sudo sandboxed-devenv host install`. For a newer package, a new sandbox key or a changed policy |
| `sandboxed-devenv client-host rm [--yes]` | control | Stops and deletes desk |
| `sandboxed-devenv client-host ssh [CMD]` | control | A shell on desk as its host user with the rig key; with CMD, runs it there. No shell expansion |
| `sandboxed-devenv new PROJECT [--repo OWNER/REPO]... [--disposable] [--deploy-key-grants-repo-write] [--cpus N] [--memory-gib N]` | control | Launches the backend for PROJECT from the base image with its own cloud-init, waits for `backend status` to report ready, runs `repo add` for each `--repo`, pins the host key, runs `connect add` on the client host, records everything in the inventory. `--repo` is optional and repeatable; repositories can be added later. `--disposable` marks the backend for stranger code, used once and destroyed (summary 4.7): `ls` flags it and `rm` takes no snapshot. `--deploy-key-grants-repo-write` applies to every `--repo` given and registers its key with write access (`gh repo deploy-key add --allow-write`) so the VM can push; the default key is read-only. `--cpus` and `--memory-gib` override the defaults in `config.yaml` (4 and 8) |
| `sandboxed-devenv ls [--json]` | control | Projects with their backend's state, address, CPUs and memory, repositories, package version, credential ages |
| `sandboxed-devenv start / stop [--force] / rm [--yes] PROJECT` | control | Lifecycle of the project's backend. `stop` asks the guest to shut down and waits, forcing it after a timeout or with `--force`. `rm` asks first (`--yes` for scripts), revokes every credential the inventory lists (M3), runs `connect rm` on the client host, then deletes the VM and the inventory record |
| `sandboxed-devenv clone PROJECT NEW_PROJECT [--yes]` | control | A disposable backend for NEW_PROJECT copied from PROJECT's disk (the source is stopped for a consistent copy, with a question first, and started again after); the copy boots with its own seed and instance id, so hostname, address, host keys and project.yaml are its own, and every repository in it gets a fresh key pair through `backend repo rekey`, registered under the new project's name (M3) |
| `sandboxed-devenv resources PROJECT [--cpus N] [--memory-gib N] [--yes]` | control | Shows the backend's CPUs and memory, or changes them. Applied live where the driver can; otherwise the command asks before stopping and starting the backend, and `--yes` answers for scripts. Disk size is future work |
| `sandboxed-devenv repo add PROJECT OWNER/REPO [--deploy-key-grants-repo-write] [--no-clone]` | control | Mints a key pair for the repository in the VM (`backend repo add`), registers it with `gh repo deploy-key add`, records it in the inventory, clones to `~/proj/PROJECT/REPO` |
| `sandboxed-devenv repo rm PROJECT OWNER/REPO [--delete-checkout]` | control | Revokes the key on GitHub, then removes it and its alias stanza in the VM; the checkout stays unless asked |
| `sandboxed-devenv repo ls PROJECT [--json]` | control | The project's repositories with key fingerprint, GitHub id, state, access and age |
| `sandboxed-devenv ssh PROJECT [CMD]` | control | Shell as `dev` in the project's backend with the rig key; with CMD, runs it there. No shell expansion, and the `--help` says so |
| `sandboxed-devenv serial-console PROJECT` | control | The backend's serial console through the driver (`screen` on UTM's pty, `incus console`), for when SSH is not up |
| `sandboxed-devenv creds ls [PROJECT] [--json]` | control | Every credential, scope, age, where it lives, how it is revoked |
| `sandboxed-devenv creds rotate PROJECT [--deploy-key [OWNER/REPO]] [--claude-token]` | control | Regenerate inside the VM (`backend repo rekey` for one repository or all), re-register, update the inventory |
| `sandboxed-devenv creds audit [--fix]` | control | Compares the inventory with GitHub's deploy keys on every repository it knows: retries pending revocations, adopts keys registered by hand, reports keys GitHub holds that no project claims, and with `--fix` deletes those whose title says this tool made them |
| `sandboxed-devenv test end-to-end` | control | End to end on real VMs, all three roles: builds or reuses the base image, creates a throwaway project, waits for its backend, runs the canary in the client and backend roles, checks SSH from `gw` to the backend and that the IDE backend service is up, compares `--version` across the three machines, then removes everything. Not covered: the interactive path (Gateway, client, preview, clipboard), provisioning the client host (`host install`), and the GitHub deploy-key flow |
| `sandboxed-devenv test host` | control | Seconds, no VM created: `host install --check` for drift and the canary in the client role on the client host |
| `sandboxed-devenv test backend PROJECT` | control | Seconds, on an existing backend: `backend status` and the canary in the backend role, for after an upgrade |
| `sandboxed-devenv host install [--dry-run] [--check]` | client host | Provisions the host: `gw` user and linger, files from the manifest, AppArmor, sysctl, nftables, Gateway tarball. Linux only, needs root |
| `sandboxed-devenv host check` | client host | Runs the canary in the client role as `gw` (through `client exec`) and prints the summary |
| `sandboxed-devenv client start` | client host | What `gw-run` does: the runtime-dir and Wayland socket ACLs, then `client session` as `gw` through sudo |
| `sandboxed-devenv client session` | client host | As `gw`: the private Xwayland with its cookie, then the Gateway launcher |
| `sandboxed-devenv client exec -- CMD` | client host | What `gw-exec` does: run a command as `gw` with its display and cookie |
| `sandboxed-devenv connect add / rm / ls` | client host | Gateway's `sshConfigs.xml` entry (Gateway reloads it when its window regains focus), the `Host` stanza in `gw`'s `ssh_config`, the pinned key in `known_hosts` |
| `sandboxed-devenv backend prepare` | backend | First boot of a project's backend: GitHub host keys, git identity, Claude settings, `IdentityAgent none`, then `backend repo add` for each repository listed in `/etc/sandboxed-devenv/project.yaml` |
| `sandboxed-devenv backend status [--json]` | backend | cloud-init done, IDE backend registered, package version, repositories with their key fingerprints, SSH host public keys |
| `sandboxed-devenv backend repo add / rm / rekey / ls` | backend | The VM side of `repo`: mint or delete a repository's key pair and alias stanza, clone or keep the checkout, print the public key as JSON |

## Installation

One mechanism per operating system, and the result is the same everywhere:
`sandboxed-devenv` on the PATH of every user that needs it.

- **Linux, which is `desk`, every backend VM and the Linux laptop:** `sudo pipx install
  --global WHEEL_OR_CHECKOUT`. The venv lands under `/opt/pipx` and the command in
  `/usr/local/bin`, readable by `jsw`, `gw`, `dev` and root, and inside sudo's
  `secure_path`, so `sudo -u gw sandboxed-devenv client session` resolves without PATH
  tricks. Ubuntu 26.04 ships pipx 1.8.0; the `--global` option arrived in 1.5.0. Confirmed
  on a fresh `desk` on 7 November: the wheel installed under Python 3.14.4, the command was
  on root's and `jsw`'s PATH, and `host install` ran from it.
- **The Mac:** `uv tool install` from the checkout, since only one user runs it there.
- **Into a VM:** over SSH, the one channel both drivers have. `render` and `image build`
  run `uv build`; the command that created the VM waits for SSH with the rig key, pushes
  the wheel, runs `sudo pipx install --global` on it (both VM users have passwordless
  sudo), then runs the role's subcommand. That happens once per base image and once for
  `desk`; a project's backend inherits the package from the base image and pushes
  nothing. Upgrading a running VM is the same push with `--force`, or a rebuild. The
  wheel never rides in a cloud-init document, and `incus file push` is not used.
- **Versions:** the version lives in `pyproject.toml` and `--version` reads it from the
  installed metadata. `doctor` and `ls` show the version on every host they can reach,
  and `test end-to-end` fails on skew.
- **Dependencies:** runtime dependencies stay at `pydantic` and `pyyaml`, both with
  prebuilt wheels for `aarch64` and `x86_64`, since every VM installs them. Anything
  heavier goes in an optional dependency group used only on the control side.

## Work items

Checkboxes are implementation steps. Items already true today are checked so the list
shows where the rig stands.

### M0: package scaffold

- [x] Command name: `sandboxed-devenv`, the same as the distribution name (decided 24
      October).
- [x] `pyproject.toml`: setuptools, `requires-python >= 3.12`, dependencies `pyyaml` and
      `pydantic`, dev group `pytest` and `build` (ruff stays a mise tool, one pin); entry
      point `sandboxed-devenv`;
      package data for `files/**`; the version in one place,
      read back by `--version` through `importlib.metadata`.
- [x] `sandboxed_devenv/cli.py` with `argparse` subparsers grouped by role as in the
      table above, each role module registering its own, `--dry-run` and `--verbose` as
      global flags, exit codes: 0 ok, 1 failure (including wrong role), 2 usage.
- [x] `uv.lock`; `.mise.toml` gains `_.python.venv` and a `sync` task (`uv sync --locked`)
      like rpkilog's; `lint-python` covers the package and a new `test` task runs pytest
      in the venv.
- [x] `render` builds the wheel with `uv build` into `build/`, beside the rendered
      artifacts; the wheel is what gets pushed into every VM from M1 on.
- [x] `tests/conftest.py` and first tests: the command tree equals the CLI table above;
      `--version` matches `pyproject.toml`; exit codes 1 and 2; `doctor` and
      `render --dry-run` run.
- [x] CI: `mise run verify` also runs `pytest` through the `test` task; the workflow
      caches uv. The macOS job stays manual.
- [x] `AGENTS.md` with the verification contract, as in rpkilog.
- [ ] `CLAUDE.md` (`@AGENTS.md`) and `.claude/settings.json` with the mise tasks and
      read-only git and gh commands allow-listed, as in rpkilog; written by the user,
      since the agent's permission classifier declines to write its own instructions.
- [x] Tools pinned in `.mise.toml`; checks defined once as mise tasks; CI runs
      `mise run verify`.

### M1: the client host as subcommands

The first payoff on both machines: real files instead of base64 blobs, the `gw` helpers
as subcommands of the one command, and one installer that runs on the Linux laptop and
inside `desk`.

- [x] Move the configuration `write_files` entries of `rehearsal/utm/desk/user-data.tmpl`
      to `files/host/` under the naming rule above, each with its header line, and write
      `files/manifest.yaml` with `dest`, `owner`, `group`, `mode`, `template` and a
      `description` per file (the descriptions in the layout above are the starting
      point). The three `gw` scripts are not moved; they are rewritten as the next item.
- [x] `tests/test_files_manifest.py`: the manifest, the tree and the headers agree, as
      described under "Files and templates"; runs in CI with the rest of pytest.
- [x] `host_client.py`: `client start` (what `gw-run` does: the empty ACL entries on
      every other runtime-dir entry, the Wayland socket ACL, then `sudo env HOME= USER=`
      to `gw` and `client session`), `client session` (as `gw`: the private Xwayland `:9`
      with an xauth cookie, no `-terminate`, then exec the Gateway launcher), and
      `client exec -- CMD` (as `gw` with `DISPLAY` and `XAUTHORITY` set). Keep the dated
      gotchas (`sudo --login` double parsing, launcher-attached profiles) as comments.
- [x] `host_install.py`: idempotent steps, each reporting changed/unchanged: create `gw`
      (no sudo, linger on), copy files from the manifest, `apparmor_parser --replace`,
      `sysctl --system`, nftables (installed, not enabled until policy says so), unpack
      the Gateway tarball for the pinned version and add the toolkit line to its
      vmoptions, write `gw`'s key pair and `ssh_config` from the secrets dir.
- [x] `sandboxed-devenv host install --dry-run` prints the plan; `--check` exits non-zero
      if anything would change (for the headless harness and for drift checks on the
      laptop).
- [x] The canary's client role gains a probe that `sandboxed-devenv` resolves for `gw`
      and that its venv under `/opt/pipx` is readable, since the sandbox now depends on
      it.
- [x] Delete `rehearsal/utm/extract-files.py` and the `extract` task; `lint-apparmor`
      and `lint-nft` read `files/` directly, with templates rendered by
      `sandboxed-devenv render`. `lint-shell` keeps linting the remaining rig scripts
      until M2 deletes them.
- [x] `files/host/cloudinit__user-data.yaml.tmpl`, the desk cloud-init, shrinks to:
      packages (plus `pipx`), the `jsw` user and the rig key. The rig script (the UTM
      driver after M2) waits for SSH, pushes the wheel, runs `sudo pipx install --global`
      and `sudo sandboxed-devenv host install`, the same two commands as the Linux
      laptop's install day. This is where pipx 1.8.0's `--global` gets confirmed on
      Ubuntu 26.04.
- [x] Rebuild `desk` from the new template; `sandboxed-devenv host check` passes with 0
      FAIL; Gateway starts via `sandboxed-devenv client start`; Markdown preview and
      clipboard still work. Done 7 November: 16 PASS, 0 FAIL; Gateway connected to the
      rehearsal backend and opened a project; preview and clipboard confirmed by hand.
- [x] The host pieces exist and are proven on `desk` as scripts: `gw-run`, `gw-session`,
      `gw-exec`, the AppArmor profiles, the runtime-dir ACLs, the private Xwayland with
      a cookie.

### M2: drivers, rendering and the backend split

Parity with today's rig, then the old scripts go.

- [x] `control_driver.py`: a `Driver` protocol with `create(spec)`, `start`, `stop`,
      `delete`, `clone`, `list`, `state`, `address(name)`, `console_command(name)`,
      `export_disk(name, dest)` and `set_resources`. The seed travels with `create`
      (a `VmSpec` with the disk, CPUs, memory and the seed directory): UTM imports drives
      only when the VM is made and Incus takes the documents as config keys at `init`, so
      the separate `deliver_seed` first planned was dropped; a VM that needs a new seed is
      recreated. `address` is None on UTM, whose Apple backend has no guest agent for
      Linux, and the seed's static address is what the inventory records. Done 8 November.
- [x] `remote.py`: `Remote` runs a command on one machine as one user with the rig key
      and nothing else (`ssh -F none`, no agent, `BatchMode`, one known_hosts file per
      machine under `~/.sandboxed_devenv/known_hosts/`, first key accepted and later
      changes refused; M3 pins keys before the first connection); `tool(args,
      parse_json=True)` runs `sandboxed-devenv` there and parses `--json`, `push`,
      `install_wheel` (to `/tmp` by an explicit path: the far shell gets the command
      quoted, so `~` would never expand, found on the first image build; the module
      states the rule as "No shell expansion", and every `--help` that takes a remote
      command repeats it), `wait_for_ssh`,
      `wait_for_cloud_init`, `wait_ready`. `Local` is the same interface for `client_host:
      local`. Done 8 November.
- [x] `control_driver_utm.py`: `create-vms.sh` and `make-seed.sh` ported: hdiutil seed
      ISO, AppleScript `make new virtual machine` with both drives imported, `utmctl`
      for start, stop, delete, clone, list and status, the serial port's pty for the
      console, `cp -c` (APFS clone) to take a disk out of a bundle, `update
      configuration` for resources (to verify with `resources`), plutil and the UTM
      relaunch for a VM with a display. The dated 4.7.5 quirks are the module docstring.
      Done 8 November.
- [x] `versions.yaml`: Gateway, IDE backend, client build, Ubuntu release and image
      serial, architecture-specific download URLs keyed by `aarch64` and `x86_64`.
      `render.py` reads it; nothing else carries a version string.
- [x] `config.yaml` model in `config.py`: `client_host`, `vm_name_prefix`, backend
      defaults (`cpus: 4`, `memory_gib: 8`, `disk_gib: 32`), and the UTM address pool
      (`192.168.64.20` upward; `.1` is the Mac, `.10` is `desk`; `.19` is the base
      image's one boot under `image build`, `utm.image_build_address`), and
      `incus_storage_dir`, the directory of the Incus `dir` pool (default: Incus's own).
      Each backend's address, CPUs and memory are recorded in the inventory when it is
      created.
- [x] Split `rehearsal/utm/backend/user-data.tmpl` into
      `files/backend/cloudinit-image__user-data.yaml.tmpl` (packages including `pipx`,
      `gh` apt source, IDE tarball and `registerBackendLocationForGateway`, plugin
      pre-install, tools, `mise` and `direnv` hooks; the package and `backend prepare
      --stage image` come over SSH from `image build`) and
      `files/backend/cloudinit-project__user-data.yaml.tmpl` (hostname, `dev`'s authorized
      key, `/etc/sandboxed-devenv/project.yaml` with the repositories, git identity and
      the Claude token, then `runcmd: sandboxed-devenv backend prepare`). The backend's
      own files (the sshd hardening, `dev`'s `ssh_config`, Claude's `settings.json`) move
      to `files/backend/` with a `stage` in the manifest, and both stages deploy them with
      the same routine `host install` uses.
- [x] `backend_prepare.py`: `backend prepare` does what the project cloud-init's
      `runcmd` does today (GitHub host keys from `api.github.com/meta`, git identity,
      Claude `settings.json`, `IdentityAgent none`, then `backend repo add` for each
      listed repository, minting keys without cloning, since the control side registers
      them first) and is idempotent, with `--check`; dev's `ssh_config` includes one
      stanza file per repository from `~/.ssh/config.d/`; the world-readable
      `prepared.yaml` carries the project name for `dev`, since `project.yaml` holds
      the token and is root-only. Proven on the rig's backend on 7 November: both
      stages applied, then 0 would change; `backend status --json` reports cloud-init
      state, the IDE backend registration, the package version, the repositories with
      their key fingerprints and the SSH host public keys; `backend repo add / rm / rekey
      / ls` as described under "Projects and repositories".
- [x] `sandboxed-devenv image build`: grow the cloud image to `disk_gib` (sparse on APFS;
      the filesystem fills it at first boot), boot it with the base cloud-init, wait for
      SSH, push and install the wheel, run `backend prepare --stage image`, then
      `cloud-init clean --logs --machine-id --configs network` and power off; on UTM the
      raw disk is kept as `~/.sandboxed_devenv/images/backend-<package version>.img` with
      `backend-<package version>.yaml` beside it (driver, architecture, package, IDE
      backend and Ubuntu versions, a digest of the rendered image cloud-init, size, build
      time), on Incus `incus publish` makes an image alias (M5). Rebuilding happens when
      any recorded input differs or with `--force`; otherwise the command says the image
      is up to date and exits 0. The build VM is `<vm_name_prefix>image-build` at
      `utm.image_build_address`; it is deleted on success and left for inspection on
      failure, and the next run removes it. The Ubuntu download is checked against the
      release's `SHA256SUMS`. Built on the Mac on 8 November: 103 s end to end (SSH after
      11 s, cloud-init 74 s, pipx 6 s, prepare 4 changed 0 failed, power off 2 s, APFS
      clone out of the bundle 0 s), 8.8 GiB allocated of 32 GiB. Two things the first
      run taught: the `~` in a remote path is never expanded because the command reaches
      the far shell quoted, and the IDE registration signal is the `userProvidedDist`
      symlink in `dev`'s cache, not the daemon's database.
- [x] `sandboxed-devenv new`, `ls`, `start`, `stop`, `rm`, `clone`, `ssh`,
      `serial-console` working on the Mac with the UTM driver: `new` hands the base
      image itself to the driver, whose import is an APFS clone (instant and
      block-sharing, confirmed 8 November), with the project's seed ISO and the CPUs and
      memory asked for; the per-project seed rendered from
      `cloudinit-project__user-data.yaml.tmpl` with a hostname derived from the VM name
      (underscores become hyphens); readiness from `backend status`; the address from the
      pool, skipping what the inventory holds and anything answering on port 22 (an older
      VM the inventory never saw). The inventory record is saved before the VM starts, so
      a failed `new` leaves something `rm` cleans up. `clone` exports the source's disk
      (stopped for the copy, with a question first) and launches it with a new seed and
      instance id, then rekeys every repository. Run on the Mac on 8 November against the
      base image of that morning: `new` ready in 17 s (import 0 s, SSH after 5 s, prepare
      11 s), `clone` in 9 s with a different key fingerprint in the copy, `stop` in 2 s,
      `resources` applied through UTM's configuration update and seen inside the guest
      (1 CPU, 3 GiB), `rm` of both with their connections gone from desk. One bug found
      by the first clone: the `backend repo` commands take no `--json` flag, so
      `Remote.tool()` gained `json_flag`. The gate run (a desk from `client-host new`,
      `new sandboxed_devenv --repo jeffsw/sandboxed_devenv`, Gateway connected) found two
      things: the project directory did not exist before the first clone, so Gateway had
      nothing to open (`backend prepare` now makes `~dev/proj/<project>` at first boot),
      and the registration hint now prints the fetch, `gh repo deploy-key add` and
      `backend repo add` commands with the real names.
- [x] `control_inventory.py` arrives with it (planned for M3): the project record with
      the backend side filled, and `repos` with the fingerprints and public keys the
      backend minted, state `unregistered` until M3 registers them.
- [x] Slow steps announce themselves: before copying a base image, downloading a cloud
      image, or waiting for a first boot, `new`, `clone`, `client-host new` and
      `image build` print what they are doing and why it takes time (for a copy: the
      size and whether the filesystem shares blocks), then the elapsed time when the
      step ends. The Incus driver passes through Incus's own progress for copies (M5).
      `common.slow_step()` does it everywhere. Done 8 November.
- [x] Routing on `desk` for several backends: the desk network-config
      (`files/host/cloudinit__network-config.yaml.tmpl`, moved into the package) carries
      two `/25` routes via the Mac, more specific than the on-link `/24`, so every
      backend is reachable without a per-backend change. Written 8 November and
      verified the same day with the two routes added live on the existing desk: gw
      reached a backend at `.21` through its `connect` stanza, with the pinned key and no
      prompt. A desk built by `client-host new` carries them from its first boot; that
      build is the one step of M2 not yet run (below). The fallback would be a `/32` per
      backend added by `connect add`.
- [x] `sandboxed-devenv resources PROJECT`: show or change a backend's CPUs and memory
      after creation. UTM: shows what the bundle's `config.plist` says and changes it
      with the backend stopped through the AppleScript configuration update. Incus:
      `incus config set` of `limits.cpu` and `limits.memory`, live where Incus allows
      (M5). When a stop is needed the command says so and asks before stopping and
      starting the backend; `--yes` answers for scripts. The inventory and `ls` follow
      the change. Done 8 November.
- [x] `host_connect.py`: `connect add PROJECT ADDRESS HOSTKEY...` writes a stanza per
      project under `/home/gw/.ssh/config.d/` (`Host <project> <address>`, user dev,
      gw's key, strict host key checking; gw's `ssh_config` includes the directory), the
      pinned host keys in `known_hosts`, and the Gateway `sshConfigs.xml` entry, the
      last only when Gateway is not running (it rewrites the file from memory on exit),
      else it prints what to do in Gateway's window; `connect rm`, `connect ls [--json]`.
      `new` and `rm` call it through `remote.py` as root on the client host; a failure
      there is reported with the command to run by hand and does not fail `new`. The
      `sshConfigs.xml` file in the manifest is now empty and marked `keep`, so `host
      install` writes it once and never overwrites the entries. Done 8 November.
- [x] `client-host new / rm / ssh` (`control_client_host.py`, not in the first plan): the
      port of `provision-desk.sh` and the desk half of `create-vms.sh`, needed before
      `rehearsal/` could go. The desk VM is `<vm_name_prefix>desk`, the login the user in
      `client_host`, the address `utm.client_host_address`; the rig key pair goes into
      the host user's secrets directory on desk, as the script did, because it is gw's
      key to the backends. Written and dry-run on 8 November; not yet run for real: a VM
      with a display needs the UTM relaunch for its input devices (4.7.5 has no reload
      command), which must happen with no VM running, and the existing desk and the old
      rehearsal backend were up. First real run: `sandboxed-devenv client-host new
      --replace` with every VM stopped, then the Gateway connection of the gate.
- [x] `policy.yaml`: client egress (VM subnet, JetBrains download and account hosts),
      backend egress (GitHub, package indexes, Anthropic), the VM subnet, the uplink
      interface and an `enforce` switch; package data beside `versions.yaml`, with
      `~/.sandboxed_devenv/policy.yaml` overriding it, since `host install` on a VM has
      no checkout. `sandboxed-devenv render` produces the host nftables ruleset from it,
      in two halves: the forward chain for the backends and an output chain for the
      sandbox user, matched by uid (nft resolves a user name when it parses the rules,
      which fails wherever `gw` does not exist, such as CI; `host install` renders the real
      uid after creating the user, `render` a placeholder). `render` without a rig key in
      the secrets directory renders a placeholder key and says so, which is how CI runs
      it. Only CIDRs reach the ruleset: nft resolves a host name at load time
      and refuses one that resolves to several addresses, which every CDN does (found
      8 November on desk), so the `hosts` lists are recorded and M5 decides the mechanism
      (a resolver-fed set, or Incus network ACLs). On the Mac the same ruleset lands on
      `desk`, where only the client half means anything, since UTM's private shared
      network cannot route the VMs through `desk` (known gap, Linux laptop only). Done
      8 November.
- [x] Replace the 4.9 command blocks in the summary with `sandboxed-devenv` commands
      (revision 12); delete `rehearsal/utm/` (`make-seed.sh`, `create-vms.sh`,
      `provision-desk.sh`, `render.py` and the templates); move `rehearsal/ci/` to
      `ci/`; the mise `render`, `schema`, `lint-yaml`, `lint-doc` and `lint-mermaid` tasks
      read `build/` only. The `lint-shell` task stays, for the bash tasks themselves.
      Done 8 November.
- [x] Templates, `render.py` with placeholder checking, `cloud-init schema` in CI, seed
      ISOs, VM creation via AppleScript, inter-VM host routes: all working on the Mac.

### M3: credentials

- [x] `control_inventory.py`: one JSON file per project in
      `~/.sandboxed_devenv/inventory/` with the project name, the backend's VM name,
      driver, image version, package version, created, a `repos` list (owner/repo,
      checkout path, access, the key's fingerprint, GitHub id and title, created,
      registered) and a `credentials` list for everything else; each entry has kind,
      scope, fingerprint or id, where it lives, created, the revoke action and its state,
      `active` or `pending-revoke`. The module and the backend side of the record came
      with M2 (8 November); the registration fields, the states and `credentials` with M3
      (8 November). A record whose backend is gone stays, marked `removed`, while a
      revocation is pending.
- [x] `control_creds.py`: deploy keys, one per repository (`backend repo add --no-clone`
      over SSH returns the public key, the key goes onto GitHub through `gh api` with
      `read_only` unless write access was asked, which returns the id to record; a second
      `backend repo add` clones; `repo rm` deletes the key on GitHub before `backend repo
      rm`; `rotate` runs `backend repo rekey`, revokes the old key and registers the new
      one; a failed GitHub call leaves the entry `pending-revoke`), Claude token (from the
      secrets dir, hashed fingerprint only in the inventory; no revoke API, so `creds ls`
      shows age and how to rotate at the account; `rotate --claude-token` rewrites the
      backend's `project.yaml` through `sudo tee` and reruns `backend prepare`), the rig
      key pair shown by `creds ls` from the secrets directory. `gh api` rather than
      `gh repo deploy-key`, since the API answers with the key's id as JSON. Done
      8 November.
- [x] `sandboxed-devenv rm` revokes every deploy key and runs `connect rm` before deleting
      the VM, and keeps the record when a revocation failed; `sandboxed-devenv clone`
      runs `backend repo rekey` for every repository in the clone and registers the new
      keys under the clone's name, rather than copying the parent's. `new --repo` fails
      before making a VM when `gh` is missing or logged out, and a registration that
      fails after the VM exists leaves the key `unregistered` with the manual commands
      printed; `repo add` retries it. Done 8 November.
- [x] `repo add`, `repo rm` and `repo ls` on the control side, and `creds audit`, which
      compares the inventory with GitHub's deploy keys on every repository it knows:
      pending revocations retried (and a removed backend's record dropped once clean),
      keys registered by hand adopted by their text (the id stored, state `active`),
      keys gone from GitHub marked `unregistered`, unclaimed keys reported, and with
      `--fix` deleted when their title starts with `sandboxed-devenv ` (keys with any
      other title, such as a CI deploy key made by hand, are reported and left alone).
      Done 8 November; unit-tested against a fake GitHub, since the agent's permission
      classifier declines to register keys itself. `repo add` ran for real the same day
      on the user's project: the key the backend had minted was registered (id recorded,
      state `active`), the repository cloned, and the JetBrains Client connected; `creds
      audit` then reported only the two stale keys of earlier backends, with foreign
      titles, left alone. Then `new m3test --repo jeffsw/sandboxed_devenv` registered its
      key and cloned by itself (ready in 19 s) and `rm m3test --yes` revoked the key before
      deleting the VM, with the audit clean afterwards apart from those two stale keys:
      the GitHub half of the M3 gate, met 8 November. Run again with Gateway closed: the
      `sshConfigs.xml` entry written by `new`, the connection listed in Gateway and
      opened, the entry removed by `rm`.
- [x] The Gateway entry while Gateway runs. `connect add` used to skip it, on the
      assumption that Gateway rewrites `sshConfigs.xml` from memory on exit and would
      drop an entry written underneath it. Tried on desk on 8 November: with Gateway open,
      `new` made a backend and `connect add` wrote its entry anyway; after a click on
      Gateway's window the connection was in its dialog, a session opened and the Client
      launched with the project; after Gateway and the Client quit, the file still held
      both entries and the header comment, so Gateway did not rewrite it at all. The IDE
      platform reloads application-level option files changed on disk when its window
      regains focus. `connect add` and `connect rm` now write the file whether or not
      Gateway runs and say, in bold yellow so it is seen, to click another window and then
      Gateway's; the `--force` flag of the experiment is gone. This closes the Gateway half of the M3
      gate: `new` needs no manual Gateway step either way.
- [x] Split the rig key in two. Until 8 November one pair, `secrets/gw_ed25519`, was both
      the control side's login to every VM (desk as `jsw`, backends as `dev`) and gw's key
      to the backends (`/home/gw/.ssh/vm_ed25519`). On the Mac rig desk's `jsw` account
      authorizes that public key, so the client sandbox user held a key that also logged
      in as the sudo-capable host user on desk: a sandbox escape in the rehearsal model,
      found 8 November while explaining `client-host new`. Not an issue on the Linux
      laptop, whose own account never authorizes the key. Now: `secrets/control_ed25519`
      (the Mac or the laptop into desk and the backends) and `secrets/sandbox_ed25519` (gw
      into the backends only); a backend's seed authorizes both, the image seed and desk's
      only the control key, `host install` installs the sandbox key (a legacy `gw_ed25519`
      with a warning), and `client-host new` pushes only the sandbox pair to desk's secrets
      directory. Migration: a `gw_ed25519` found becomes the control key, since it is what
      the control side has been logging in with, and the sandbox key is minted beside it;
      desk then needs `client-host provision` (the SSH half of `client-host new` on an
      existing desk: wheel, sandbox key, `host install`), and a backend made before the
      split is reachable by gw again after `rm` and `new`. `creds ls` shows both pairs.
      Done 8 November, and run through by the user the same day: `client-host provision`
      changed only gw's key pair on desk, the old backend was removed, a new one made with
      both keys, and Gateway connected to it with the sandbox key.
- [x] Host key pinning, done the other way round: instead of reading the VM's host key
      out after boot (the serial console on UTM, the guest on Incus), the control side
      mints the ed25519 host key pair into the seed with cloud-init's `ssh_keys`
      (`ssh_deletekeys: true`, `ssh_genkeytypes: [ed25519]`, so no other key exists),
      pins the public half in `~/.sandboxed_devenv/known_hosts/<vm>` before the VM boots
      and hands it to `connect add`; `remote.py` checks host keys strictly everywhere,
      and `accept-new` is gone. The private half exists only inside the seed, where the
      VM's root reads it; the control side drops it once the seed is written. Works for
      backends, the build VM and desk; the image record's digest of the image cloud-init
      is taken with fixed host key values, or every build would look new. Done
      8 November: the rebuilt image's first boot and a backend from it both connected
      under strict checking with the pinned key. The backend still had RSA and ECDSA host
      key files beside it (left by the image's first boot or made by Ubuntu's service), so
      the backend's `sshd_config.d/10-backend.conf` names the ed25519 key as the only
      `HostKey`, and sshd offers nothing unpinned.
- [x] Secrets dir: `~/.sandboxed_devenv/secrets/` (mode 0700) holding the two key pairs
      and `claude_oauth_token`, one file per secret and none of them in `config.yaml`; a
      file holding an `op://vault/item/field` reference is resolved with the 1Password CLI
      (`op read`) when the secret is read, so the reference may live in config management
      while the value never does; a missing or signed-out `op` is reported before any VM is
      made. `sandboxed-devenv doctor` reports each secret's source (absent, file or the
      reference), the per-project token files it finds, the token policy and whether `op`
      is on the PATH. Done 8 November.
- [x] Policy knob in the user config: `backend.claude_token` is `shared` (the one token in
      `secrets/claude_oauth_token` for every backend, the default, as the open question in
      the summary suggested), `per-project` (`secrets/claude_oauth_token.<project>`, none
      when absent) or `none`; and `backend.disposable_claude_token`, off by default, since a
      backend for stranger code should not carry the account's token. `new`, `clone` (always
      disposable) and `creds rotate --claude-token` follow it and say which token they used
      and why. Done 8 November.
- [x] The deploy key per VM, `IdentityAgent none`, the Claude token in `settings.json`,
      and the pre-provisioned Gateway connection are proven on the rig; registration is
      still manual (summary 4.9, "manual steps").

### M4: testing

- [ ] `canary.py` gains `--json` output (one object per probe plus a summary) and keeps
      the human output; exit code non-zero on any FAIL (already) and a `--expect FILE`
      mode that compares against a golden result for the role.
- [ ] Unit tests: renderer golden files for both VMs and both architectures; drivers
      against recorded `utmctl list` and `incus list --format json` output; `remote.py`
      against recorded `--json` output; inventory round-trips; `policy.yaml` to nftables
      checked with `nft --check` on Linux; `host install --dry-run` plans.
- [ ] `sandboxed-devenv test end-to-end`: builds or reuses the image,
      `sandboxed-devenv new ci-<run>`, waits, runs the canary in both roles through
      `remote.py`, checks SSH from `gw` to the backend, that the backend service is
      present, and that `--version` agrees on the control side, the client host and the
      backend, then `sandboxed-devenv rm`. Runs on the Mac on demand (`mise run test-e2e`)
      and later on the Linux laptop; never in GitHub CI. `test host` and
      `test backend PROJECT` are the cheap siblings: no VM is created and they run in
      seconds.
- [ ] The interactive checklist in `docs/runbook.md`: Gateway, client, Wayland toolkit in
      About, Markdown preview, clipboard both ways, Claude Code `/status`, git fetch
      without a key dialog, plugin install path. This is the part that stays manual.
- [ ] `lint-doc` house rules extended to `PLAN.md`, `README.md` and `docs/`.
- [ ] Warnings in bold yellow wherever a terminal shows them: `common.warning()` colours
      the `connect add` and `connect rm` notices about a running Gateway (8 November,
      asked for by the user), and the control side forwards the setting to a command it
      runs on another machine (`SANDBOXED_DEVENV_COLOR=always` over SSH, since the far
      side sees a pipe; `NO_COLOR` honoured). Extend it to the other warning-like lines:
      a failed registration or `connect` in `new`, pending revocations in `rm` and `ls`,
      the leftover build VM in `image build`, `resources` announcing a stop, unclaimed
      keys in `creds audit`. (The running-Gateway line stays bold yellow although nothing
      is wrong, because the user has something to do: defocus and refocus the window.)
- [x] Static verification in CI: schema, AppArmor and nftables parsers, shell, Python,
      YAML, workflow, doc rules and Mermaid rendering.

### M5: the Linux laptop

Written before the laptop arrives, first run on it.

- [ ] `control_driver_incus.py`: `incus launch images:ubuntu/<release>/cloud <vm> --vm`
      with `cloud-init.user-data` and `cloud-init.network-config` config keys, `incus
      publish` for the base image, `incus copy` for clones (a full copy on a `dir` pool
      without reflinks, reported as a slow step), `incus list --format json` for state
      and addresses, `incus console` for the serial console, `incus file pull` for the
      host keys. The pool is `dir` on `incus_storage_dir`, created by `host install` if
      absent.
- [ ] Host nftables from `policy.yaml` covering both halves: the `gw` uid for the client
      and the forward chain on the Incus bridge for the VMs. Incus network ACLs are the
      alternative if the bridge rules fight with Incus's own firewall management; decide
      on the laptop.
- [ ] `docs/linux-laptop-install.md`: Ubuntu 26.04 (per Framework's guide on the 13 Pro),
      full-disk encryption, no separate volume (the Incus pool is a directory on the
      root filesystem; only free space matters), the operations user or VM for router
      access (summary section 5), then the package:
      `sudo pipx install --global ~/proj/ide_sandbox/sandboxed_devenv`,
      `client_host: local` in the config, `sudo sandboxed-devenv host install`,
      `sandboxed-devenv image build`, `sandboxed-devenv new` for the first project,
      `sandboxed-devenv host check`.
- [ ] Run `sandboxed-devenv test end-to-end` on the laptop; record timings in the summary
      (step 6 of 4.9 asked for them).
- [ ] The Linux laptop's AppArmor and userns state is the same Ubuntu 26.04 default as
      `desk`; verify the profiles load and the client's embedded browser works before
      declaring the host done.

### M6: later

- [ ] Mermaid inside the Markdown preview: the preview itself renders on the rebuilt desk
      (7 November), but a Mermaid block in it was not tried; check it once the
      implementation milestones are done, since it takes the same JCEF path.
- [ ] Adjustable backend disk size after creation: grow the raw disk or the Incus volume,
      then the filesystem inside, through `resources --disk-gib`.
- [ ] Snapshots before disposal (`sandboxed-devenv snapshot PROJECT`), so uncommitted work
      in a project VM is recoverable for a while; a full copy on a `dir` pool, reported
      as a slow step.
- [ ] Version bump workflow: change `versions.yaml` or the package version,
      `sandboxed-devenv image build`, `sandboxed-devenv ls` shows which backends run an
      older image or package; `sandboxed-devenv upgrade PROJECT` recreates from the new
      image with the same credentials policy, or pushes the new wheel and reinstalls when
      only the package changed. Noted 8 November: the rebuild decision compares versions
      and the rendered cloud-init, so a code change without a version bump leaves the
      image and every backend on the old code until `image build --force`; a digest of
      the wheel in the record would catch it.
- [ ] Cloud and service CLIs per project (`aws`, Linode, Terraform) and their scoped
      credentials, the open question in the summary.
- [ ] Section 5 hardening on the host: the 1Password agent allowlist, nothing
      router-related in the development principal.
- [ ] The agent-level sandbox inside backend VMs (summary, "Deferred work"), once the rest
      is routine.
- [ ] A release channel (a PyPI release or a Debian package) if pushing the wheel and
      installing from the checkout proves tiresome.

## Order and gates

| Milestone | Gate to pass | Where |
|---|---|---|
| M0 scaffold | `mise run verify` green with the package installed and one test | CI |
| M1 client host | `desk` rebuilt from the slim template with the package installed; `sandboxed-devenv host check` 0 FAIL; Gateway via `client start`; preview and clipboard work | Mac |
| M2 drivers and backend split | `sandboxed-devenv new` to a connected client on the Mac, the package installed in the backend; old scripts deleted. Status 8 November: `new` ready in 17 s with the connection written for gw on desk, gw's SSH to the backend proven, scripts deleted; left: a desk from `client-host new` and the connection opened in Gateway's window | Mac |
| M3 credentials | `sandboxed-devenv new` and `sandboxed-devenv rm` need no manual GitHub or Gateway step. Met 8 November: `new --repo` registered and cloned, its connection appeared in Gateway's list and worked, `rm` revoked the key and removed the entry; with Gateway open, the entry appears after a click on its window and survives its exit | Mac |
| M4 testing | `mise run test-e2e` passes on the Mac unattended, versions matching on all three machines | Mac |
| M5 Linux laptop | `sandboxed-devenv test end-to-end` passes on the laptop; first real project opened | Linux laptop |

M1 through M4 run on the Mac rig as it exists today, two UTM VMs, no nested
virtualization. The Mac remains the place to test changes after the Linux laptop is the
daily machine, which is the reason the UTM driver is kept rather than retired.

## Open decisions

- [x] **Command name.** `sandboxed-devenv`, the same as the distribution name, decided
      24 October; both were free on PyPI and Homebrew as of 23 October. Rejected: `devenv`
      (devenv.sh, Nix), `devbox` (Jetify), and the short `sdev`, which PyPI already has
      as an active serial-console toolkit that installs a command of that name.
- [x] **Package install.** Decided 1 November: `pipx install --global` on Linux (`desk`,
      every backend, the Linux laptop), `uv tool install` on the Mac, and the wheel pushed
      into each VM over SSH (see "Wheel delivery"). Rejected: `uv tool install` from the
      private repository on the Linux laptop (per user, so invisible to `gw`), and a bare
      `pip install` of the wheel (refused by Ubuntu's externally managed Python).
- [x] **State dir.** Decided 7 November: one directory, `~/.sandboxed_devenv/`, holding
      `config.yaml`, `inventory/` (one JSON file per project) and `secrets/` (mode 0700,
      one file per secret). Secrets never appear in `config.yaml`, only optional `op://`
      references, so configuration and secrets can be managed separately. Rejected: the
      two XDG locations, which spread one tool's state over two trees.
- [x] **Base image on UTM.** Decided 7 November: the base image is a raw disk file,
      `~/.sandboxed_devenv/images/backend-<version>.img`, and each backend is an APFS
      clone of it attached to a VM the driver creates through AppleScript with the
      project's seed and resources, as the rig does today. Rejected: `utmctl clone` of a
      stopped base VM, whose copy keeps the base seed drive and needs plist edits and a
      UTM relaunch afterwards. Settled with it: disk size from `config.yaml` (default
      32 GiB, sparse), static addresses from a pool, two `/25` routes on `desk`, and
      backend defaults of 4 CPUs and 8 GiB with `--cpus` and `--memory-gib` on `new` and
      `resources`. Adjustable disk size is future work (M6).
- [x] **Wheel delivery.** Decided 7 November: over SSH with the rig key, for both
      drivers, by the command that created the VM: once into the base image, once into
      `desk`, never into a project's backend, which inherits the package. Rejected: the
      wheel as a base64 entry in cloud-init (large documents, a size question on Incus,
      failures visible only on the serial console), and `incus file push` (a second
      path for one driver when SSH serves both).
- [x] **Binding a key to its repository inside the VM.** Decided 7 November: an alias
      stanza per repository (`Host github.com-<owner>-<repo>`, `IdentitiesOnly yes`), one
      file each under `dev`'s `~/.ssh/config.d/` which `ssh_config` includes, written by
      `backend repo add`, with the checkout's remote
      pointing at the alias; it works for any git command from any directory. Rejected:
      `core.sshCommand` set per checkout, which keeps plain URLs but applies only inside
      that checkout, so submodules and fresh clones miss it.
- [x] **Incus storage.** Decided 7 November: a `dir` pool on whatever filesystem the
      laptop already has, in a directory set by `config.yaml`, so no filesystem type is
      required and install day needs no partitioning. Each backend's disk is a raw file
      in it, as on the Mac. The guest owns its filesystem behind a virtio block device
      whatever the pool type, so isolation is unaffected. Costs: `clone` and snapshots
      are full copies where the filesystem has no reflinks (ext4), and Incus cannot
      enforce per-backend disk quotas on `dir`. Reversible: a btrfs pool, even one on a
      loop file in the same directory, can be added later and backends moved to it.
      Rejected: btrfs or ZFS on a dedicated volume, which would prescribe a filesystem.
- [x] **Host distribution.** Decided 7 November: Ubuntu 26.04 on the Linux laptop, the
      same release as the VMs (AppArmor profiles, pipx 1.8.0, Framework's certification).
      Changing it would reopen M1.
