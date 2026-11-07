# sandboxed_devenv

Design notes and tooling for a sandboxed development environment: the IDE
thin client runs in a small sandbox on the host, and the toolchain, IDE
backend, and any AI agent run in per-project VMs with controlled egress.

- `ide_sandboxing_summary.md`: the design record (start here)
- `PLAN.md`: the implementation plan: one Python package, `sandboxed_devenv`, with one command,
  `sandboxed-devenv`, installed on the control machine, the client host and every backend VM
- `sandboxed_devenv/`: that package. `mise run sync` builds the venv; `--version`, `doctor`,
  `render`, `canary`, `host install` / `host check` and `client start` / `session` / `exec`
  work today, the other subcommands are stubs that name their milestone. `files/` under it
  holds everything a role receives on disk, with a manifest
- `AGENTS.md`: conventions and the verification contract for humans and agents
- `ide_sandboxing.html`: original brainstorming document, kept for history
- `rehearsal/utm/`: the Mac rig (summary section 4.9): `make-seed.sh`, `create-vms.sh`, `provision-desk.sh` and the backend's cloud-init; the desk cloud-init now lives in the package
- `mise run verify`: renders `files/` and the seeds and runs every check CI runs (`cloud-init schema`, AppArmor and nftables syntax on Linux; shellcheck, ruff, yamllint, actionlint, doc rules, Mermaid and pytest everywhere). Tools are pinned in `.mise.toml`; tasks live in `.mise/tasks/`.
