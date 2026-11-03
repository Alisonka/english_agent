# AGENTS.md

Details: `README.md` (architecture, env vars), `CONTRIBUTING.md` (branches, commits, releases).

## Communication
- Reply to the user in Russian. Code, comments, commit messages and docs in English.
- Be brief: lead with the result, no restating the task, no recap of code you just read.
- If the request is ambiguous, ask one short question instead of guessing.

## Verification: only when asked
- Do NOT run tests, linters, formatters or type checks (`pytest`, `ruff`, `mypy`, `pre-commit`) after making changes unless the user explicitly asks.
- When asked, run the narrowest form first: `uv run pytest tests/<file>.py -q`, then the full suite only if requested.
- Full gate (CI runs the same): `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.

## Token economy
- Read only what the task needs: search with `rg` first, then read the relevant line range, not whole files.
- Don't re-read files already in context; don't explore the repo "just in case".
- Prefer quiet commands (`-q`, `--stat`, `| tail`) to avoid long output.
- Make minimal diffs: change only what the task requires, no drive-by refactors or reformatting.
- Don't create new docs/markdown files unless asked.

## Code conventions
- Python 3.12, dependencies via `uv` only (never `pip`). Run tools as `uv run ...`.
- All functions fully typed (mypy `disallow_untyped_defs`). Line length 120.
- Match surrounding code style; comments only for non-obvious constraints.
- Data shapes are pydantic models in `src/english_agent/models/`.
