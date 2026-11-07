# sandboxed_devenv: agent instructions

`PLAN.md` is the implementation plan (layout, CLI surface, milestones, decisions);
`ide_sandboxing_summary.md` is the design record. Read the relevant part before changing code.

## Verification

Verification is defined once as **mise tasks** (`.mise/tasks/`), so the same checks run for
Claude, for humans and in GitHub CI. Tool versions are pinned in `.mise.toml`.

- `mise run verify`: everything CI checks: render, cloud-init schema, AppArmor and nftables
  syntax, shellcheck, ruff, yamllint, actionlint, the doc rules, Mermaid, and pytest.
- `mise run test`: pytest in the project venv; extra arguments after `--`, e.g. `-- -k cli`.
- `mise run sync`: `uv sync --locked`, the venv with the package (editable) and the dev group.
- `mise run lint-python`: ruff plus a syntax check of every Python file.
- `mise run render`: `sandboxed-devenv render` (files/ and the wheel under `build/`) and the
  cloud-init seeds under `rehearsal/utm/build/`, which the other checks read.

The Linux-only validators (`cloud-init schema`, `apparmor_parser`, `nft`) are skipped on macOS
with a notice; CI runs on Linux and requires them.

After a set of changes, run `mise run verify` on your own initiative and report the result.
Don't ask which verification to run.

## git

- Commit messages: a one-line subject, then bullet points rather than prose when there is
  more to say.
- Never commit `build/` or any key material; secrets live in `~/.sandboxed_devenv/secrets/`,
  outside the checkout, and never enter rendered artifacts, CI output or commits. Don't repeat a secret in chat if one was printed by mistake.
- Don't merge to `main`; the user opens and merges pull requests.

## Python

- setuptools, `argparse`, `pydantic` for models, `pytest` with importlib mode, one flat package
  with role prefixes (`control_`, `host_`, `backend_`); see `PLAN.md`, "Target layout".
- Line length: 100 is the soft limit, 120 the hard limit for new code (`ruff.toml` allows 160
  only because of the older rig scripts).
- Prefer traditional loops over list and dict comprehensions.
- Store a function's return value in a variable before returning it (`retval`, `retstr`,
  `retlist`, `retdict` are fine names), so a debugger breakpoint can inspect it.
- Docstrings on Enum members describe the values.
- A new subcommand goes in its role module, in the CLI table of `PLAN.md`, and in the
  `PLANNED_COMMANDS` list of `tests/test_cli.py`, which fails when the three disagree.

## Documents

`PLAN.md`, `ide_sandboxing_summary.md` and `README.md` follow the house rules that
`mise run lint-doc` checks on the summary: no em-dashes (use commas, colons or parentheses),
every table row with the header's column count, prose wrapped at about 90 columns. The summary
keeps a revision history at the top; bump it when its content changes.

## Reviewing pull requests or branches

- Fetch the change set with `gh pr view`, `gh pr diff` or `git diff` / `git log` / `git show`;
  read surrounding code with the Read and Grep tools.
- Verification is only the mise tasks; don't invent ad-hoc lint or test commands. Spend review
  effort on what those checks cannot catch.
- Actionable findings as Markdown checkboxes; non-actionable remarks as bullets.
