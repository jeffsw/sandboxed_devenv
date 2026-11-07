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
| control | the Mac | the laptop | `image`, `new`, `ls`, `start`, `stop`, `rm`, `clone`, `repo`, `ssh`, `serial-console`, `creds`, `test` |
| client host | the `desk` VM | the laptop | `host`, `client`, `connect` |
| backend | each backend VM under UTM | each backend VM under Incus | `backend` |
| any | every machine above | every machine above | `doctor`, `render`, `canary`, `--version` |

The user config names the client host: `client_host: local` on the Linux laptop,
`client_host: ssh://jsw@192.168.64.10` on the Mac. `doctor` reports which roles the
machine it runs on can play, and a subcommand run in the wrong place says so and exits 1.

## Projects and repositories

A **project** is the unit of work: a directory `~/proj/<project>` holding one or more
repositories, the layout used on the Mac. A **backend** is the VM that serves one
project, and there is exactly one per project, so every command that needs a backend
takes the project's name as `PROJECT` and finds the backend through the inventory; no
command takes a backend name. The VM itself is named after the project with a prefix,
`vm_name_prefix` in the user config, default `idesbx-`, so `utmctl list` and
`incus list` show `idesbx-ide_sandbox` apart from other VMs on a shared hypervisor.
Inside the VM the repositories are checked out at `~/proj/PROJECT/<repo>`, the same
layout as outside, and the Gateway connection opens `~/proj/PROJECT`, where the IntelliJ
project spans them. Repositories come and go over the life of the backend: `new` takes
none or several, `repo add` and `repo rm` change the set later. A disposable backend is
a project too, a short-lived one with a name to match.

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
  commands it would run, and can be re-run safely.
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
  config.py                      user config + versions + policy models (pydantic), YAML readers
  render.py                      template rendering and the wheel build, no side effects
  canary.py                      stdlib-only probes; the `canary` subcommand; runnable with python3 -m
  remote.py                      runs the command on the client host or a backend over SSH, parses --json
  control_driver.py              the Driver protocol
  control_driver_utm.py          utmctl, osascript, plutil, hdiutil (from create-vms.sh, make-seed.sh)
  control_driver_incus.py        incus CLI with --format json
  control_images.py              image build / ls / rm
  control_backends.py            new, ls, start, stop, rm, clone, ssh, serial-console: one backend per project
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
      cloudinit__user-data.yaml.tmpl            the desk VM on the Mac rig: install the wheel, host install
      etc__apparmor.d__jetbrains-client.apparmor
      etc__nftables.conf.tmpl
      etc__sysctl.d__99-vmrouter.conf
      home__gw__.ssh__config.ssh_config.tmpl
      home__gw__.config__JetBrains__JetBrainsGateway@@GATEWAY_BRANCH@@__options__sshConfigs.xml.tmpl
    backend/
      cloudinit-image__user-data.yaml.tmpl      the base image: packages, IDE backend, plugin, tools, the wheel
      cloudinit-project__user-data.yaml.tmpl    a project's backend: hostname, keys, project config, prepare
      cloudinit__network-config.yaml.tmpl
      etc__ssh__sshd_config.d__10-backend.conf
      home__dev__.ssh__config.ssh_config
      home__dev__.claude__settings.json.tmpl
tests/                           pytest, one test module per package module, as in rpkilog
ci/                              puppeteer config for the Mermaid check (from rehearsal/ci/)
docs/
  runbook.md                     install, new project, dispose, rotate, troubleshoot
  linux-laptop-install.md        the laptop's first day, step by step
policy.yaml                      egress allowlists and the VM subnet
versions.yaml                    Gateway, IDE backend, client, Ubuntu image serial, tool versions
```

`rehearsal/` goes away: its scripts are replaced by the package and deleted once M2
reaches parity, the desk template becomes `files/host/cloudinit__user-data.yaml.tmpl`,
and `rehearsal/ci/` moves to `ci/`. `ide_sandboxing_summary.md` stays the design record;
its 4.9 commands get replaced by `sandboxed-devenv` equivalents at M2.

### Files and templates

Everything a role receives on disk, and every cloud-init document, lives under
`files/<role>/` under one naming rule, so a listing says where a file goes, what format
it is and whether it is rendered, and the file itself says so too.

- **The directory is the role.** The filename is the destination with the leading slash
  dropped and every other slash replaced by `__`. Dots stay, since none is leading once
  joined: `home__gw__.ssh__config`. Cloud-init documents have no destination, so they
  take the prefix `cloudinit__` and the NoCloud file name, which Incus also uses for its
  config keys; the backend role has `cloudinit-image__` for the base image and
  `cloudinit-project__` for a project's backend.
- **The format is the extension.** The destination's own when it has one, otherwise an
  added one from a fixed set: `.apparmor`, `.ssh_config`, `.yaml` for cloud-init. `.tmpl`
  comes last and marks a template; everything else is copied byte for byte.
- **A variable part of a destination is written as `@@NAME@@`,** upper case, at the
  position it occupies in the path, exactly as placeholders are written inside
  templates. The filename then decodes to the manifest's `dest` character for
  character, and the value comes from `versions.yaml` or the project config at deploy
  time. Gateway's directory carries its branch, so its file is
  `home__gw__.config__JetBrains__JetBrainsGateway@@GATEWAY_BRANCH@@__options__sshConfigs.xml.tmpl`.
  A variable in the path says nothing about the content; `.tmpl` is still decided by
  whether the content is rendered.
- **`manifest.yaml` is the authority** for what a file cannot say about itself: `dest`
  (placeholders allowed), `owner`, `group`, `mode` (quoted, since YAML reads a bare
  `0644` as an octal integer), `template`, a one-line `description`, and for backend
  files the `stage`, `image` or `project`, at which `backend prepare` deploys them.
- **Every file says where it goes.** Each file whose format has comments opens with a
  line naming the role and destination, such as
  `# sandboxed-devenv host: rendered to /etc/nftables.conf`. Cloud-init needs
  `#cloud-config` first, so those documents carry it on line two; JSON has no comments,
  so `settings.json` relies on the manifest alone.
- **A unit test keeps the three in step:** every manifest `source` exists, every file
  under `files/` other than the manifest has an entry, every filename decodes to its
  entry's `dest`, and every header agrees with its entry. It runs in CI from M1 on.

## CLI surface

| Command | Role | Does |
|---|---|---|
| `sandboxed-devenv --version` | any | The package version; it must agree on every machine or `test end-to-end` fails |
| `sandboxed-devenv doctor` | any | Which roles this machine can play, driver and tool versions, config and secrets dirs, the version on each client host and backend it can reach |
| `sandboxed-devenv render [--out DIR]` | any | Renders every artifact (cloud-inits, nftables, AppArmor, ssh configs) and builds the wheel, without touching a VM. CI's entry point |
| `sandboxed-devenv canary --role ROLE [--json]` | any | The probes for the client or backend role, run locally |
| `sandboxed-devenv image build [--force]` | control | Downloads the Ubuntu cloud image, boots it with the base cloud-init (which installs the package), waits, runs `cloud-init clean`, powers off, registers the base image |
| `sandboxed-devenv image ls / rm` | control | Base images and their versions |
| `sandboxed-devenv new PROJECT [--repo OWNER/REPO]... [--disposable] [--deploy-key-grants-repo-write]` | control | Launches the backend for PROJECT from the base image with its own cloud-init, waits for `backend status` to report ready, runs `repo add` for each `--repo`, pins the host key, runs `connect add` on the client host, records everything in the inventory. `--repo` is optional and repeatable; repositories can be added later. `--disposable` marks the backend for stranger code, used once and destroyed (summary 4.7): `ls` flags it and `rm` takes no snapshot. `--deploy-key-grants-repo-write` applies to every `--repo` given and registers its key with write access (`gh repo deploy-key add --allow-write`) so the VM can push; the default key is read-only |
| `sandboxed-devenv ls` | control | Projects with their backend's state, address, repositories, package version, credential ages |
| `sandboxed-devenv start / stop / rm PROJECT` | control | Lifecycle of the project's backend; `rm` revokes every credential the inventory lists and runs `connect rm`, then deletes the VM |
| `sandboxed-devenv clone PROJECT NEW_PROJECT` | control | A disposable backend for NEW_PROJECT copied from PROJECT's; every repository in the copy gets a fresh key pair through `backend repo rekey`, registered under the new project's name |
| `sandboxed-devenv repo add PROJECT OWNER/REPO [--deploy-key-grants-repo-write] [--no-clone]` | control | Mints a key pair for the repository in the VM (`backend repo add`), registers it with `gh repo deploy-key add`, records it in the inventory, clones to `~/proj/PROJECT/REPO` |
| `sandboxed-devenv repo rm PROJECT OWNER/REPO [--delete-checkout]` | control | Revokes the key on GitHub, then removes it and its alias stanza in the VM; the checkout stays unless asked |
| `sandboxed-devenv repo ls PROJECT` | control | The project's repositories with key fingerprint, access and age |
| `sandboxed-devenv ssh PROJECT [CMD]` | control | Shell as `dev` in the project's backend with the rig key |
| `sandboxed-devenv serial-console PROJECT` | control | The backend's serial console through the driver (`screen` on UTM's pty, `incus console`), for when SSH is not up |
| `sandboxed-devenv creds ls [PROJECT]` | control | Every credential, scope, age, where it lives, how it is revoked |
| `sandboxed-devenv creds rotate PROJECT [--deploy-key [OWNER/REPO]] [--claude-token]` | control | Regenerate inside the VM (`backend repo rekey` for one repository or all), re-register, update the inventory |
| `sandboxed-devenv creds audit [--fix]` | control | Compares the inventory with `gh repo deploy-key list` on every repository it knows: retries pending revocations, reports keys GitHub holds that no project claims, and with `--fix` deletes them |
| `sandboxed-devenv test end-to-end` | control | End to end on real VMs, all three roles: builds or reuses the base image, creates a throwaway project, waits for its backend, runs the canary in the client and backend roles, checks SSH from `gw` to the backend and that the IDE backend service is up, compares `--version` across the three machines, then removes everything. Not covered: the interactive path (Gateway, client, preview, clipboard), provisioning the client host (`host install`), and the GitHub deploy-key flow |
| `sandboxed-devenv test host` | control | Seconds, no VM created: `host install --check` for drift and the canary in the client role on the client host |
| `sandboxed-devenv test backend PROJECT` | control | Seconds, on an existing backend: `backend status` and the canary in the backend role, for after an upgrade |
| `sandboxed-devenv host install [--dry-run] [--check]` | client host | Provisions the host: `gw` user and linger, files from the manifest, AppArmor, sysctl, nftables, Gateway tarball. Linux only, needs root |
| `sandboxed-devenv host check` | client host | Runs the canary in the client role as `gw` (through `client exec`) and prints the summary |
| `sandboxed-devenv client start` | client host | What `gw-run` does: the runtime-dir and Wayland socket ACLs, then `client session` as `gw` through sudo |
| `sandboxed-devenv client session` | client host | As `gw`: the private Xwayland with its cookie, then the Gateway launcher |
| `sandboxed-devenv client exec -- CMD` | client host | What `gw-exec` does: run a command as `gw` with its display and cookie |
| `sandboxed-devenv connect add / rm / ls` | client host | Gateway's `sshConfigs.xml` entry, the `Host` stanza in `gw`'s `ssh_config`, the pinned key in `known_hosts` |
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
  tricks. Ubuntu 26.04 ships pipx 1.8.0; the `--global` option arrived in 1.5.0. Not yet
  confirmed on the rig (both VMs were stopped when the plan was revised); M1 does that.
- **The Mac:** `uv tool install` from the checkout, since only one user runs it there.
- **Into a VM:** `render` and `image build` run `uv build`; the wheel rides in the
  cloud-init as one gzip-and-base64 `write_files` entry (tens of kilobytes, well within
  NoCloud and Incus limits) and `runcmd` installs it before the role's subcommand runs.
  Upgrading a running VM is `pipx install --global --force` of a wheel pushed over SSH,
  or a rebuild.
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
- [ ] `pyproject.toml`: setuptools, `requires-python >= 3.12`, dependencies `pyyaml` and
      `pydantic`, dev group `pytest`, `ruff`, `build`; entry point `sandboxed-devenv`;
      package data for `files/**`; the version in one place,
      read back by `--version` through `importlib.metadata`.
- [ ] `sandboxed_devenv/cli.py` with `argparse` subparsers grouped by role as in the
      table above, each role module registering its own, `--dry-run` and `--verbose` as
      global flags, exit codes: 0 ok, 1 failure (including wrong role), 2 usage.
- [ ] `uv.lock`; `.mise.toml` gains `_.python.venv = ".venv"` and a `uv sync` task like
      rpkilog's; `lint-python` and a new `test` task run inside the venv.
- [ ] `render` builds the wheel with `uv build` into `build/`, beside the rendered
      artifacts; the wheel is what every cloud-init carries from M1 on.
- [ ] `tests/conftest.py` and first tests: `sandboxed-devenv --help` lists every
      subcommand; `--version` matches `pyproject.toml`.
- [ ] CI: `mise run verify` also runs `pytest`; the workflow installs the package in the
      venv. The macOS job stays manual.
- [ ] `AGENTS.md` with the verification contract, as in rpkilog; `CLAUDE.md` includes it.
- [x] Tools pinned in `.mise.toml`; checks defined once as mise tasks; CI runs
      `mise run verify`.

### M1: the client host as subcommands

The first payoff on both machines: real files instead of base64 blobs, the `gw` helpers
as subcommands of the one command, and one installer that runs on the Linux laptop and
inside `desk`.

- [ ] Move the configuration `write_files` entries of `rehearsal/utm/desk/user-data.tmpl`
      to `files/host/` under the naming rule above, each with its header line, and write
      `files/manifest.yaml` with `dest`, `owner`, `group`, `mode`, `template` and a
      `description` per file (the descriptions in the layout above are the starting
      point). The three `gw` scripts are not moved; they are rewritten as the next item.
- [ ] `tests/test_files_manifest.py`: the manifest, the tree and the headers agree, as
      described under "Files and templates"; runs in CI with the rest of pytest.
- [ ] `host_client.py`: `client start` (what `gw-run` does: the empty ACL entries on
      every other runtime-dir entry, the Wayland socket ACL, then `sudo env HOME= USER=`
      to `gw` and `client session`), `client session` (as `gw`: the private Xwayland `:9`
      with an xauth cookie, no `-terminate`, then exec the Gateway launcher), and
      `client exec -- CMD` (as `gw` with `DISPLAY` and `XAUTHORITY` set). Keep the dated
      gotchas (`sudo --login` double parsing, launcher-attached profiles) as comments.
- [ ] `host_install.py`: idempotent steps, each reporting changed/unchanged: create `gw`
      (no sudo, linger on), copy files from the manifest, `apparmor_parser --replace`,
      `sysctl --system`, nftables (installed, not enabled until policy says so), unpack
      the Gateway tarball for the pinned version and add the toolkit line to its
      vmoptions, write `gw`'s key pair and `ssh_config` from the secrets dir.
- [ ] `sandboxed-devenv host install --dry-run` prints the plan; `--check` exits non-zero
      if anything would change (for the headless harness and for drift checks on the
      laptop).
- [ ] The canary's client role gains a probe that `sandboxed-devenv` resolves for `gw`
      and that its venv under `/opt/pipx` is readable, since the sandbox now depends on
      it.
- [ ] Delete `rehearsal/utm/extract-files.py` and the `extract` task; `lint-apparmor`
      and `lint-nft` read `files/` directly, with templates rendered by
      `sandboxed-devenv render`. `lint-shell` keeps linting the remaining rig scripts
      until M2 deletes them.
- [ ] `files/host/cloudinit__user-data.yaml.tmpl`, the desk cloud-init, shrinks to:
      packages (plus `pipx`), the `jsw` user, the rig key, the wheel in one `write_files`
      entry, and `runcmd`: `pipx install --global` the wheel, then
      `sandboxed-devenv host install`. This is where pipx 1.8.0's `--global` gets
      confirmed on Ubuntu 26.04.
- [ ] Rebuild `desk` from the new template; `sandboxed-devenv host check` passes with 0
      FAIL; Gateway starts via `sandboxed-devenv client start`; Markdown preview and
      clipboard still work.
- [x] The host pieces exist and are proven on `desk` as scripts: `gw-run`, `gw-session`,
      `gw-exec`, the AppArmor profiles, the runtime-dir ACLs, the private Xwayland with
      a cookie.

### M2: drivers, rendering and the backend split

Parity with today's rig, then the old scripts go.

- [ ] `control_driver.py`: a `Driver` protocol with `create(name, image, cpus,
      memory, seed)`, `start`, `stop`, `delete`, `clone`, `list`, `address(name)`,
      `console(name)`, and `deliver_seed(name, dir)` because UTM wants an ISO attached as
      a drive and Incus wants config keys.
- [ ] `remote.py`: run `sandboxed-devenv ...` on the client host (locally when
      `client_host: local`, else over SSH) and on a backend (over SSH as `dev` with
      the rig key), returning parsed `--json` output. `wait_ready(name)` polls
      `backend status --json` through it.
- [ ] `control_driver_utm.py`: port `create-vms.sh` (AppleScript create, plutil for
      keyboard, pointer and clipboard, the UTM relaunch) and `make-seed.sh` (hdiutil
      ISO). Keep the documented UTM 4.7.5 quirks as comments with dates.
- [ ] `versions.yaml`: Gateway, IDE backend, client build, Ubuntu release and image
      serial, architecture-specific download URLs keyed by `aarch64` and `x86_64`.
      `render.py` reads it; nothing else carries a version string.
- [ ] Split `rehearsal/utm/backend/user-data.tmpl` into
      `files/backend/cloudinit-image__user-data.yaml.tmpl` (packages including `pipx`,
      `gh` apt source, IDE tarball and `registerBackendLocationForGateway`, plugin
      pre-install, tools, `mise` and `direnv` hooks, the wheel and its
      `pipx install --global`, then `sandboxed-devenv backend prepare --stage image`) and
      `files/backend/cloudinit-project__user-data.yaml.tmpl` (hostname, `dev`'s authorized
      key, `/etc/sandboxed-devenv/project.yaml` with the repositories, git identity and
      the Claude token, then `runcmd: sandboxed-devenv backend prepare`). The backend's
      own files (the sshd hardening, `dev`'s `ssh_config`, Claude's `settings.json`) move
      to `files/backend/` with a `stage` in the manifest, and both stages deploy them with
      the same routine `host install` uses.
- [ ] `backend_prepare.py`: `backend prepare` does what the project cloud-init's
      `runcmd` does today (GitHub host keys from `api.github.com/meta`, git identity,
      Claude `settings.json`, `IdentityAgent none`, then `backend repo add` for each
      listed repository) and is idempotent; `backend status --json` reports cloud-init
      state, the IDE backend registration, the package version, the repositories with
      their key fingerprints and the SSH host public keys; `backend repo add / rm / rekey
      / ls` as described under "Projects and repositories".
- [ ] `sandboxed-devenv image build`: boot the cloud image with the base cloud-init, wait,
      `cloud-init clean --logs`, power off; UTM keeps the raw disk under `build/images/`
      named by version, Incus publishes an image alias. Rebuilding is forced by a version
      change or `--force`.
- [ ] `sandboxed-devenv new`, `ls`, `start`, `stop`, `rm`, `clone`, `ssh`,
      `serial-console` working on the Mac with the UTM driver; the per-project seed
      rendered from `cloudinit-project__user-data.yaml.tmpl`; readiness from
      `backend status`.
- [ ] `host_connect.py`: `connect add PROJECT ADDRESS HOSTKEY` writes the Gateway
      `sshConfigs.xml` entry (only when Gateway is not running, else print instructions),
      the `Host` stanza in `gw`'s `ssh_config` and the pinned host key in `known_hosts`;
      `connect rm`, `connect ls`. `new` and `rm` call it through `remote.py`.
- [ ] `policy.yaml`: client egress (VM subnet, JetBrains download and account hosts),
      backend egress (GitHub, package indexes, Anthropic), the VM subnet.
      `sandboxed-devenv render` produces the host nftables ruleset from it; on the Mac the
      same ruleset lands on `desk` for the client half only, since UTM's private shared
      network cannot route the VMs through `desk` (known gap, Linux laptop only).
- [ ] Replace the 4.9 command blocks in the summary with `sandboxed-devenv` commands;
      delete `rehearsal/utm/` (`make-seed.sh`, `create-vms.sh`, `render.py` and the
      templates) and the `lint-shell` task with it; move `rehearsal/ci/` to `ci/`; the
      mise `render` and `schema` tasks call `sandboxed-devenv render`.
- [x] Templates, `render.py` with placeholder checking, `cloud-init schema` in CI, seed
      ISOs, VM creation via AppleScript, inter-VM host routes: all working on the Mac.

### M3: credentials

- [ ] `control_inventory.py`: one JSON file per project under the state dir with the
      project name, the backend's VM name, driver, image version, package version,
      created, a `repos` list (owner/repo, checkout path, access, the key's fingerprint
      and GitHub id, created) and a `credentials` list for everything else; each entry has
      kind, scope, fingerprint or id, where it lives, created, the revoke action and its
      state, `active` or `pending-revoke`.
- [ ] `control_creds.py`: deploy keys, one per repository (`backend repo add` over SSH
      returns the public key, `gh repo deploy-key add` with `--allow-write` when asked,
      record the key id; `repo rm` runs `gh repo deploy-key delete` before `backend repo
      rm`; `rotate` runs `backend repo rekey` and re-registers; a failed GitHub call
      leaves the entry `pending-revoke`), Claude token (from the
      secrets dir, hashed fingerprint only in the inventory; no revoke API, so `creds ls`
      shows age and the runbook says how to rotate at the account), `gw`'s key pair
      (minted once per host, recorded with its fingerprint), git identity.
- [ ] `sandboxed-devenv rm` revokes everything listed and runs `connect rm` before
      deleting the VM; `sandboxed-devenv clone` runs `backend repo rekey` for every
      repository in the clone and registers the new keys under the clone's name, rather
      than copying the parent's.
- [ ] `repo add`, `repo rm` and `repo ls` on the control side, and `creds audit`, which
      compares the inventory with `gh repo deploy-key list` on every repository it
      knows: pending revocations retried, unclaimed keys reported, `--fix` deletes them.
- [ ] Host key pinning: `new` takes the VM's SSH host public key through the driver
      (Incus can read it from the guest; UTM's serial console carries cloud-init's host
      key print) before the first SSH connection, then hands it to `connect add`; never
      trust on first use.
- [ ] Secrets dir: `~/.config/sandboxed-devenv/secrets/` (mode 0700) holding the rig key
      pair and `claude_oauth_token`; optional `op://` references resolved with the
      1Password CLI at render time. `sandboxed-devenv doctor` reports which is in use.
- [ ] Policy knob in the user config: one Claude token for all VMs or one per trust
      level; default one token, as the open question in the summary suggests.
- [x] The deploy key per VM, `IdentityAgent none`, the Claude token in `settings.json`,
      and the pre-provisioned Gateway connection are proven on the rig; registration is
      still manual (summary 4.9, "manual steps").

### M4: testing

- [ ] `canary.py` gains `--json` output (one object per probe plus a summary) and keeps
      the human output; exit code non-zero on any FAIL (already) and a `--expect FILE`
      mode that compares against a golden result for the role.
- [ ] Unit tests: renderer golden files for both VMs and both architectures (the wheel
      entry masked, since it changes with every version); drivers against recorded
      `utmctl list` and `incus list --format json` output; `remote.py` against recorded
      `--json` output; inventory round-trips; `policy.yaml` to nftables checked with
      `nft --check` on Linux; `host install --dry-run` plans.
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
- [x] Static verification in CI: schema, AppArmor and nftables parsers, shell, Python,
      YAML, workflow, doc rules and Mermaid rendering.

### M5: the Linux laptop

Written before the laptop arrives, first run on it.

- [ ] `control_driver_incus.py`: `incus launch images:ubuntu/<release>/cloud <vm> --vm`
      with `cloud-init.user-data` and `cloud-init.network-config` config keys, `incus
      publish` for the base image, `incus copy` for clones, `incus list --format json`
      for state and addresses, `incus console` for the serial console, `incus file pull`
      for the host keys.
- [ ] Host nftables from `policy.yaml` covering both halves: the `gw` uid for the client
      and the forward chain on the Incus bridge for the VMs. Incus network ACLs are the
      alternative if the bridge rules fight with Incus's own firewall management; decide
      on the laptop.
- [ ] `docs/linux-laptop-install.md`: Ubuntu 26.04 (per Framework's guide on the 13 Pro),
      full-disk encryption, a separate volume for the Incus storage pool, the operations
      user or VM for router access (summary section 5), then the package:
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

- [ ] Snapshots before disposal (`sandboxed-devenv snapshot PROJECT`), so uncommitted work
      in a project VM is recoverable for a while.
- [ ] Version bump workflow: change `versions.yaml` or the package version,
      `sandboxed-devenv image build`, `sandboxed-devenv ls` shows which backends run an
      older image or package; `sandboxed-devenv upgrade PROJECT` recreates from the new
      image with the same credentials policy, or pushes the new wheel and reinstalls when
      only the package changed.
- [ ] Cloud and service CLIs per project (`aws`, Linode, Terraform) and their scoped
      credentials, the open question in the summary.
- [ ] Section 5 hardening on the host: the 1Password agent allowlist, nothing
      router-related in the development principal.
- [ ] The agent-level sandbox inside backend VMs (summary, "Deferred work"), once the rest
      is routine.
- [ ] A release channel (a PyPI release or a Debian package) if carrying the wheel in
      cloud-init and installing from the checkout proves tiresome.

## Order and gates

| Milestone | Gate to pass | Where |
|---|---|---|
| M0 scaffold | `mise run verify` green with the package installed and one test | CI |
| M1 client host | `desk` rebuilt from the slim template with the package installed; `sandboxed-devenv host check` 0 FAIL; Gateway via `client start`; preview and clipboard work | Mac |
| M2 drivers and backend split | `sandboxed-devenv new` to a connected client on the Mac, the package installed in the backend; old scripts deleted | Mac |
| M3 credentials | `sandboxed-devenv new` and `sandboxed-devenv rm` need no manual GitHub or Gateway step | Mac |
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
      every backend, the Linux laptop), `uv tool install` on the Mac, and the wheel
      carried in each VM's cloud-init. Rejected: `uv tool install` from the private
      repository on the Linux laptop (per user, so invisible to `gw`), and a bare
      `pip install` of the wheel (refused by Ubuntu's externally managed Python).
- [ ] **State dir.** `~/.local/state/sandboxed-devenv/` for the inventory and
      `~/.config/sandboxed-devenv/` for config and secrets, or everything under one
      directory. Default proposed: the two XDG locations.
- [ ] **Base image on UTM.** A raw disk copied per backend (simple, what the rig does
      today) versus `utmctl clone` of a stopped base VM (faster, but UTM copies the whole
      bundle). Proposed: raw disk copy, since UTM's clone is not the Linux laptop's path.
- [ ] **Wheel delivery to Incus VMs.** The cloud-init entry (uniform with UTM) versus
      `incus file push` after launch (no size concern, one more driver step). Proposed:
      the cloud-init entry, with the push as the fallback if Incus objects to the size.
- [ ] **Binding a key to its repository inside the VM.** An alias stanza per repository
      in `dev`'s `ssh_config` (works for any git command from any directory, needs the
      alias in the remote URL) versus `core.sshCommand` set per checkout (plain URLs, but
      only inside that checkout; submodules and fresh clones miss it). Proposed: the
      alias stanza, written by `backend repo add`, with the checkout's remote pointing
      at it.
- [ ] **Incus storage.** Directory, btrfs or ZFS pool on the Linux laptop; affects the
      partitioning on install day. Proposed: btrfs on its own volume.
- [ ] **Host distribution.** Ubuntu 26.04 is assumed throughout (AppArmor profiles,
      Framework's certification). Changing it reopens M1.
