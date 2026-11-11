# Contributing

Thanks for contributing to english-agent. This guide covers how to set up your
environment and the conventions we follow for collaborative development.

## Development environment

Requirements: Python >= 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies (including dev tools) into a local .venv
uv sync

# Copy the env template and add your Anthropic API key
cp .env.example .env

# Install the git pre-commit hooks (one-time)
uv run pre-commit install
```

Never commit `.env` or any real secrets. The file is already in `.gitignore`.

## Pre-commit hooks

After `uv run pre-commit install`, the hooks in
[.pre-commit-config.yaml](.pre-commit-config.yaml) run automatically on every
`git commit` against the files you staged: `ruff` lint (with autofix),
`ruff format`, and basic file hygiene (trailing whitespace, end-of-file,
YAML/TOML validity, large files, merge conflicts, private keys). If a hook
changes files or fails, the commit is aborted - review and `git add` the fixes,
then commit again. To run them on the whole repo manually:

```bash
uv run pre-commit run --all-files
```

## Running tests

```bash
uv run pytest
```

## Linting, formatting and type checking

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting and
[mypy](https://mypy.readthedocs.io/) for static type checking. Their
configuration lives in [pyproject.toml](pyproject.toml).

```bash
uv run ruff check .          # lint
uv run ruff format .         # auto-format
uv run ruff format --check . # verify formatting (what CI runs)
uv run mypy                  # type-check the english_agent package
```

Please make sure linting, formatting, type checking and the test suite all pass
before opening a pull request (CI runs the same checks).

## Branching model

We use a trunk-based workflow with short-lived branches:

- `main` is always releasable and is protected (no direct pushes).
- Create a branch off `main` for each change, using a type prefix:
  - `feat/<short-description>` for new features
  - `fix/<short-description>` for bug fixes
  - `chore/<short-description>` for maintenance (CI, deps, tooling)
  - `docs/<short-description>` for documentation-only changes
- Open a pull request into `main`; merge after review and green CI.

```bash
git switch -c feat/my-change
# ... work, commit ...
git push -u origin feat/my-change
gh pr create --fill
```

## Commit messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.

Examples:

- `feat(research): add skysmart as a source`
- `fix(writer): retry once on validation error`
- `docs: document environment variables`

## Changelog and releases

The changelog and releases are automated by
[release-please](https://github.com/googleapis/release-please) from the commit
history, so **do not edit [CHANGELOG.md](CHANGELOG.md) or the version by hand**.
This is why [Conventional Commits](#commit-messages) matter: `feat:` bumps the
minor version, `fix:` the patch, and `feat!:` / `BREAKING CHANGE:` the major
(while the project stays pre-1.0, breaking changes bump the minor).

The project follows [Semantic Versioning](https://semver.org/). The version
lives in `version` in [pyproject.toml](pyproject.toml) and `__version__` in
`src/english_agent/__init__.py`; release-please keeps both in sync.

How a release happens:

1. On every push to `main`, the `release-please` workflow opens (or updates) a
   "release PR" that collects the pending changes into CHANGELOG.md and bumps the
   version.
2. Merge that PR when you want to cut the release. release-please then creates the
   `vX.Y.Z` tag and the GitHub release automatically.
