# sandboxed_devenv

Design notes and tooling for a sandboxed development environment: the IDE
thin client runs in a small sandbox on the host, and the toolchain, IDE
backend, and any AI agent run in per-project VMs with controlled egress.

- `ide_sandboxing_summary.md`: the design record (start here)
- `PLAN.md`: the implementation plan: one Python package, `sandboxed_devenv`, with one command,
  `sandboxed-devenv`, installed on the control machine, the client host and every backend VM
- `sandboxed_devenv/`: that package. `mise run sync` builds the venv. The control side
  (`image build`, `client-host new`, `new`, `ls`, `start`, `stop`, `rm`, `clone`, `resources`,
  `ssh`, `serial-console`), the client host (`host install` / `check`, `client start` /
  `session` / `exec`, `connect add` / `rm` / `ls`), the credentials (`repo add` / `rm` / `ls`,
  `creds ls` / `rotate` / `audit`, deploy keys registered and revoked through `gh`), the
  backend (`backend prepare` / `status` / `repo`) and the harnesses (`test end-to-end` /
  `host` / `backend`) all work. Two key pairs live in `~/.sandboxed_devenv/secrets/`: the
  control key for the control machine into every VM, the sandbox key for `gw` into the
  backends only; every VM's SSH host key is minted into its seed and pinned before boot
- `docs/runbook.md`: install, secrets, a project's life, disposal, rotation, the harnesses,
  the interactive checklist, troubleshooting
- `docs/linux-laptop-install.md`: the Linux laptop's first day, written before it arrived `files/` under it holds everything a role receives on disk, with a manifest;
  `versions.yaml` pins every version and `policy.yaml` is the egress policy
- `AGENTS.md`: conventions and the verification contract for humans and agents
- `ide_sandboxing.html`: original brainstorming document, kept for history
- `ci/`: the Mermaid CLI's browser configuration for the doc check
- `mise run verify`: renders `files/` and runs every check CI runs (`cloud-init schema`, AppArmor and nftables syntax on Linux; shellcheck, ruff, yamllint, actionlint, doc rules, Mermaid and pytest everywhere). Tools are pinned in `.mise.toml`; tasks live in `.mise/tasks/`.
- `mise run test-e2e`: the three roles on real VMs on this machine, a throwaway project made and removed; never in GitHub CI.
