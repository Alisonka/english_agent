# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Telegram delivery (`src/english_agent/telegram/`): a `Lesson` is rendered as a
  scannable Telegram HTML message — emoji-anchored sections, an always-visible
  "суть" (key idea), the detailed theory in a collapsible `<blockquote expandable>`
  with bold paragraph labels, highlighted constructions in bold, compact one-line
  examples, and sources as links. Long lessons are packed into several messages on
  whole-section boundaries (keeping the theory quote intact) under the 4096-character
  limit, and sent through the Bot API. The pipeline sends by default; `--no-send`
  only prints the JSON. `python -m english_agent.telegram chats|ping` helps find the
  chat id and test delivery. New settings: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Daily schedule (`.github/workflows/daily-lesson.yml`, 15-minute timeout): without
  `--topic` the pipeline picks the topic of the day from `CURRICULUM_START_DATE`,
  announces the end of the course once, and reports failed lessons to the chat.
- Component versioning for the pipeline (`src/english_agent/versions.py`): a
  central registry of schema versions and prompt content fingerprints, plus a
  `Provenance` passport (pipeline version, component, schema version, LLM model,
  prompt fingerprints, curriculum version, timestamp).
- `Lesson` and `ResearchResult` now carry an optional `provenance` field recording
  exactly how each artifact was produced, for reproducibility and traceability.
- Developer tooling: `ruff` (lint + format) and `mypy` (type checking) with
  configuration in `pyproject.toml`, plus a `.python-version` file.
- Pre-commit hooks (`.pre-commit-config.yaml`): ruff lint + format and basic file
  hygiene checks, run automatically before each commit.

### Changed

- `Lesson` schema reshaped for readable messenger delivery (schema version 1 -> 2):
  new required `key_idea_ru` (one-sentence core idea, shown first and never hidden),
  `rule_ru` shortened (max 1200) and written as short labelled paragraphs, and
  `nuances_ru` trimmed to 3-4 short items (max 200 chars each). The
  `write_lesson_system` prompt was updated to match.
- Formatted the codebase with `ruff format` and fixed lint / type findings.

### Deprecated

### Removed

### Fixed

### Security

## [0.1.0] - 2026-10-22

### Added

- Research agent that gathers A2 English grammar material from curated sites
  (`src/english_agent/research/`).
- Lesson writer that turns research into daily A2 grammar lessons
  (`src/english_agent/writer.py`).
- Pipeline tying research and writing together (`src/english_agent/pipeline.py`).
- Anthropic (Claude) LLM client (`src/english_agent/llm/`).
- Runtime configuration via environment variables / `.env`
  (`src/english_agent/config.py`), with `.env.example` template.
- Curriculum topic files for A2 and B1 levels (`curriculum/`).
- Evaluation datasets (`evals/datasets/`).
- Test suite covering the client, writer, research agent, sitemaps, and stage-1 spec
  (`tests/`).

[Unreleased]: https://github.com/Alisonka/english_agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Alisonka/english_agent/releases/tag/v0.1.0
