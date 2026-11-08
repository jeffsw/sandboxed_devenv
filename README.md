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
  `session` / `exec`, `connect add` / `rm` / `ls`) and the backend (`backend prepare` /
  `status` / `repo`) work today; `repo`, `creds` and `test` are stubs that name their
  milestone. `files/` under it holds everything a role receives on disk, with a manifest;
  `versions.yaml` pins every version and `policy.yaml` is the egress policy
- `AGENTS.md`: conventions and the verification contract for humans and agents
- `ide_sandboxing.html`: original brainstorming document, kept for history
- `ci/`: the Mermaid CLI's browser configuration for the doc check
- `mise run verify`: renders `files/` and runs every check CI runs (`cloud-init schema`, AppArmor and nftables syntax on Linux; shellcheck, ruff, yamllint, actionlint, doc rules, Mermaid and pytest everywhere). Tools are pinned in `.mise.toml`; tasks live in `.mise/tasks/`.
