# sandboxed_devenv

Design notes and tooling for a sandboxed development environment: the IDE
thin client runs in a small sandbox on the host, and the toolchain, IDE
backend, and any AI agent run in per-project VMs with controlled egress.

- `ide_sandboxing_summary.md`: current design and plan (start here)
- `ide_sandboxing.html`: original brainstorming document, kept for history
- `rehearsal/utm/`: cloud-init files and scripts for the two rehearsal VMs on the Mac (summary section 4.9)
