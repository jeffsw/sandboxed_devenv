# sandboxed_devenv

Design notes and tooling for a sandboxed development environment: the IDE
thin client runs in a small sandbox on the host, and the toolchain, IDE
backend, and any AI agent run in per-project VMs with controlled egress.

- `ide_sandboxing_summary.md`: the design record (start here)
- `PLAN.md`: the implementation plan: one Python package, `sandboxed_devenv`, with one command,
  `sandboxed-devenv`, installed on the control machine, the client host and every backend VM
- `sandboxed_devenv/`: that package. `mise run sync` builds the venv; `sandboxed-devenv --help`,
  `--version`, `doctor` and `render` work today, the other subcommands are stubs that name
  their milestone
- `AGENTS.md`: conventions and the verification contract for humans and agents
- `ide_sandboxing.html`: original brainstorming document, kept for history
- `rehearsal/utm/`: cloud-init files and scripts for the two rehearsal VMs on the Mac (summary section 4.9)
- `mise run verify`: renders the seeds and runs every check CI runs (`cloud-init schema`, AppArmor and nftables syntax on Linux; shellcheck, ruff, yamllint, actionlint, doc rules, Mermaid and pytest everywhere). Tools are pinned in `.mise.toml`; tasks live in `.mise/tasks/`.
