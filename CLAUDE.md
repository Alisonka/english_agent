# CLAUDE.md

Details: `README.md` (architecture, env vars), `CONTRIBUTING.md` (branches, commits, releases).

## Server
- The main project lives on a remote server; everything is deployed and run there.
- Connect with `ssh vpn`.

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

## Git
- Never add a `Co-authored-by: Cursor <cursoragent@cursor.com>` trailer (or any co-author trailer) to commits.
- Large tasks: split the work into logically independent groups of changes and land each group separately
  before starting the next one:
  1. Start from a clean tree and branch off `main`: `git switch main && git switch -c <type>/<short-description>`
     (`feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`, see `CONTRIBUTING.md`).
  2. Make only that group's changes, then commit them with a Conventional Commits message that
     describes the whole group (`<type>(<scope>): <summary>`).
  3. Only after the commit is made, move on to the next group on a new branch.
- Never mix unrelated changes in one branch or commit. If a task fits in one commit, a single branch is enough.

## Versioning of pipeline components
Component versions live in one place: `src/english_agent/versions.py` (see README, "Provenance").
Every change must keep them in sync with the code, in the same commit as the change:
- Changed the shape of a model in `src/english_agent/models/` (new/removed/renamed field, changed meaning)?
  Bump its entry in `SCHEMA_VERSIONS` and extend the inline comment (`# vN: what changed`).
  New model that is stored or attached to pipeline outputs? Add an entry starting at `1`.
- Added a prompt in `src/english_agent/prompts/`? Register it in `PROMPT_FILES`. Edited prompts need
  no bump: fingerprints are derived from the file content.
- Changed a storage table (`storage/orm.py`)? Add an Alembic migration in `migrations/versions/`
  (next sequential number); never edit an existing migration.
- Added a new pipeline component or stage? Attach a `Provenance` to its output via `build_provenance`,
  with its own `component` name and schema version.
- Don't bump the package version (`pyproject.toml` / `__version__`) unless asked: that happens at release time.
- Before finishing a task, check `git diff --stat` for touched models, prompts and ORM tables and confirm
  the corresponding version entries were updated.
