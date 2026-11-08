# Runbook

How to run the sandboxed development environment day to day: install it, make a project,
work in it, dispose of it, rotate its credentials, and find out what is wrong when something
is. The design is in `ide_sandboxing_summary.md`, the plan in `PLAN.md`; this is the short
version for the keyboard. Every command is `sandboxed-devenv ...` on the control machine (the
Mac or the laptop) unless it says otherwise, and every mutating command takes `--dry-run`.

## Install

On the Mac rig, section 4.9 of the summary has the full sequence. In short:

```sh
brew install --cask utm && brew install qemu gh
uv tool install ~/proj/ide_sandbox/sandboxed_devenv
mkdir -p ~/.sandboxed_devenv && printf 'client_host: ssh://jsw@192.168.64.10\n' > ~/.sandboxed_devenv/config.yaml
gh auth login
sandboxed-devenv image build          # the base image; about two minutes
sandboxed-devenv client-host new      # the desk VM; ten minutes or more, then log in at its window
```

On the Linux laptop, `docs/linux-laptop-install.md` (M5). The laptop is its own client host:
`client_host: local`, `sudo pipx install --global` of the wheel, `sudo sandboxed-devenv host
install`, `image build`.

`sandboxed-devenv doctor` says what the machine can do, where its state lives, which secrets
and keys exist and where they come from, and whether `gh` is logged in.

## Secrets

`~/.sandboxed_devenv/secrets/` (mode 0700) holds one file per secret:

| File | What | Who has it |
|---|---|---|
| `control_ed25519` | the control machine's SSH key into desk and every backend | the control machine only |
| `sandbox_ed25519` | gw's SSH key into the backends | the control machine; desk as `/home/gw/.ssh/vm_ed25519` |
| `claude_oauth_token` | the Claude Code token from `claude setup-token` | every backend, in `/home/dev/.claude/settings.json` |
| `claude_oauth_token.<project>` | a token for one project, under the `per-project` policy | that project's backend |

A file may hold an `op://vault/item/field` reference instead of the value; the 1Password CLI
resolves it when the secret is read. `backend.claude_token` in `config.yaml` is `shared`
(default), `per-project` or `none`; disposable backends get no token unless
`backend.disposable_claude_token` is on.

## A project

```sh
sandboxed-devenv new PROJECT --repo OWNER/REPO [--repo ...] [--deploy-key-grants-repo-write] [--disposable]
```

In about twenty seconds: the backend VM from the base image, its SSH host key minted and
pinned, the connection written for gw and Gateway on the client host, a deploy key per
repository minted in the VM and registered on GitHub, the repositories cloned under
`/home/dev/proj/PROJECT/`. Then on the client host, `sandboxed-devenv client start` in the
GNOME session runs Gateway; the connection is listed under SSH, and the project directory is
`/home/dev/proj/PROJECT`. If Gateway was already open, click another window and then
Gateway's: it reloads its connection list on focus.

Day to day:

```sh
sandboxed-devenv ls                        # projects, state, address, resources, repositories
sandboxed-devenv ssh PROJECT [-- CMD]      # a shell as dev, or one command. No shell expansion
sandboxed-devenv stop PROJECT              # a clean shutdown; --force pulls the plug
sandboxed-devenv start PROJECT
sandboxed-devenv resources PROJECT --cpus 6 --memory-gib 12   # stops and starts the backend, asks first
sandboxed-devenv repo add PROJECT OWNER/REPO                  # another repository, key registered and cloned
sandboxed-devenv repo rm PROJECT OWNER/REPO [--delete-checkout]
sandboxed-devenv clone PROJECT SCRATCH     # a disposable copy with fresh deploy keys
```

## Dispose

```sh
sandboxed-devenv rm PROJECT [--yes]
```

Revokes every deploy key on GitHub, removes the connection from the client host, deletes the
VM and the inventory record. A revocation that fails keeps the record, marked removed, until
`sandboxed-devenv creds audit` gets it through. Uncommitted work in the VM is gone with it;
`clone` first if any of it matters.

## Rotate

```sh
sandboxed-devenv creds ls [PROJECT]                       # every credential, its age, where it lives, how it is revoked
sandboxed-devenv creds rotate PROJECT --deploy-key [OWNER/REPO]   # fresh key pair, old key revoked, new one registered
sandboxed-devenv creds rotate PROJECT --claude-token      # the token from the secrets directory into the backend
sandboxed-devenv creds audit [--fix]                      # the inventory against GitHub; --fix deletes this tool's unclaimed keys
sandboxed-devenv client-host provision                    # a new wheel, a new sandbox key or a changed policy onto desk
```

To replace the control or sandbox key pair: replace the files in the secrets directory, then
`client-host provision` (sandbox key) or `client-host new --replace` (control key), and `rm`
plus `new` for every backend.

## Test

```sh
sandboxed-devenv test host                 # seconds: drift check and the client canary on the client host
sandboxed-devenv test backend PROJECT      # seconds: status and the backend canary
mise run test-e2e                          # minutes: a throwaway project through all three roles, then removed
```

The client canary runs as gw and expects a GNOME session on the client host in which `client
start` has run; otherwise its Wayland and runtime-directory probes fail.

### The interactive checklist

What no harness covers, to run by hand after a change to the client host or the IDE
versions:

1. Gateway starts from `client start` and lists the backend's connection.
2. The connection opens without a host key prompt; the JetBrains Client downloads and starts.
3. Help, About in the Client reports the Wayland toolkit (`sun.awt.wl.WLToolkit`); the Gateway
   launcher's log says the same.
4. The Markdown preview renders (the embedded browser has its private X server and AppArmor
   profiles); a Mermaid block in it renders too (M6).
5. The clipboard works both ways between the Mac and the Client.
6. Claude Code in the backend terminal: `/status` shows the token as the credential.
7. `git fetch` in a checkout works without a key dialog (the deploy key through its alias;
   `IdentityAgent none` keeps the IDE's proxied agent away).
8. A plugin installed from the Client lands in the backend after the backend restarts.

## Troubleshoot

- **A backend does not come up.** `sandboxed-devenv serial-console PROJECT` shows cloud-init's
  console (quit `screen` with ctrl-a k); `sandboxed-devenv ssh PROJECT -- cloud-init status
  --long` and `-- sandboxed-devenv backend status` once SSH answers.
- **Gateway does not list a connection.** On desk, `sudo sandboxed-devenv connect ls` shows
  what was written; if Gateway was open when `new` ran, defocus and refocus its window.
- **A host key warning.** Every VM's key is pinned before it boots in
  `~/.sandboxed_devenv/known_hosts/<vm>`, so a mismatch means the VM was rebuilt outside the
  tool or something is in the way; `rm` and `new` the project.
- **GitHub refuses a push.** Deploy keys are read-only unless `new` or `repo add` ran with
  `--deploy-key-grants-repo-write`; `repo ls PROJECT` shows the access.
- **A code change does not reach new backends.** The base image carries the package and is
  rebuilt only when a version or the image cloud-init changes; `image build --force` after a
  code change to `backend prepare`, then `new`.
- **The audit reports unclaimed keys.** Keys with this tool's title prefix are left by
  backends that no longer exist (`--fix` deletes them); keys with other titles are yours.
- **desk cannot reach a new backend.** desk routes both halves of the VM subnet through the
  Mac from its first boot; a desk from before November 8 needs `client-host new --replace`.
