# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3](https://github.com/Alisonka/english_agent/compare/v0.1.2...v0.1.3) (2026-11-20)


### Added

* **bot:** Interlocutor answers questions on studied material ([114d873](https://github.com/Alisonka/english_agent/commit/114d873daa8e90fe091f5ea1fd8a7011316a9898))
* **bot:** learner goal and interests commands ([c56dd16](https://github.com/Alisonka/english_agent/commit/c56dd16292dbc18b80d86ead31935eeba8c87b6a))
* Critic, review Planner and Reflector ([4a3b569](https://github.com/Alisonka/english_agent/commit/4a3b569de0a7a5edeed059ae201b68d76545104d))
* **critic:** starting difficulty estimate for every bank question ([1f88876](https://github.com/Alisonka/english_agent/commit/1f88876a484f139e711c04c2774dcb3695c3a4b0))
* **curriculum:** prerequisite graph with load-time validation and the learner's frontier ([21bb07c](https://github.com/Alisonka/english_agent/commit/21bb07cc69a88cee23a3c97c6d40830f11d9b74b))
* **evals:** synthetic learners with a hidden knowledge map ([5a24013](https://github.com/Alisonka/english_agent/commit/5a240133c545856f341237904ef77ee07a9bbde4))
* **exam:** Examiner blueprint, adaptive delivery by code, report ([dd578f7](https://github.com/Alisonka/english_agent/commit/dd578f7d5bc93e6b94339b21e883385b0346a2c3))
* **generation:** a Critic checks every step and cheat sheet; one runner owns the content run ([ff212ac](https://github.com/Alisonka/english_agent/commit/ff212ac8d3f26af541f9504b2ed04a6d2389ee08))
* **generation:** Exercise composer for probes, contrast pairs and drills ([ccaca9e](https://github.com/Alisonka/english_agent/commit/ccaca9ee90629c83f0a599d0adca572728461462))
* **generation:** remedial and contrast briefs with shared keys, ordered as background jobs ([045032a](https://github.com/Alisonka/english_agent/commit/045032a37e0b5fe7ee69d0a63a0a693179b96fba))
* **grading:** Arbiter re-grades disputes independently and compensates ([b843a7e](https://github.com/Alisonka/english_agent/commit/b843a7edf11840b717a42d82e6fa6c621bc73e70))
* **learner:** ability and question difficulty (Elo) moved by every graded answer ([1f716d2](https://github.com/Alisonka/english_agent/commit/1f716d22d0a781f2645ce53bf8e6b016dc4bb60c))
* **learner:** element and topic status from memory, mastery and marks ([9e7ecae](https://github.com/Alisonka/english_agent/commit/9e7ecae20f4b1c00c3a7c5adc3f6517e9d179060))
* **mentor:** journal with checkable expectations, hypotheses, weekly dossier ([ac4b81e](https://github.com/Alisonka/english_agent/commit/ac4b81edbd7a02cb4fdb05b0507aee181245573f))
* **mentor:** orchestrator session with tools, limits, shadow mode and fallback ([f6e1454](https://github.com/Alisonka/english_agent/commit/f6e1454399564b3576483d952df77022db7a3cfb))
* **onboarding:** adaptive placement test that builds the knowledge map ([baab3f9](https://github.com/Alisonka/english_agent/commit/baab3f962c376bed41c57025f2ed556690611c2b))
* **planning:** day plan as a document, deterministic fallback policy, morning executes the plan ([869dee0](https://github.com/Alisonka/english_agent/commit/869dee059dfcb598d15c255f54c8b21e5b17a851))
* **planning:** policy layer and plan dry-run ([ce2693b](https://github.com/Alisonka/english_agent/commit/ce2693b8f228265fdcf22405a1f8d88c74c5655d))
* **practice:** a Planner picks the day's reviews from the learner's own cards ([069acac](https://github.com/Alisonka/english_agent/commit/069acac579aaaf42dbaa29775990f2ecef7b2cfa))
* **profile:** a Reflector reads each learner's new errors and steers practice and feedback ([6b34c1e](https://github.com/Alisonka/english_agent/commit/6b34c1ee8db4a31b40cc9948a624befff4801b05))
* **profile:** Diagnostician with read tools replaces the one-shot Reflector ([09ebb2d](https://github.com/Alisonka/english_agent/commit/09ebb2d75bf02d4bfa2db419ebc0d39d6d526bce))
* **storage:** one evidence table every agent decision must cite ([94b4d3d](https://github.com/Alisonka/english_agent/commit/94b4d3de6cacc87009385cbf688d7ab85f7a70e4))


### Fixed

* **ci:** green the gate on SQLAlchemy 2.1, fsrs 6.3 and aiogram 3.31 typing ([6890498](https://github.com/Alisonka/english_agent/commit/6890498a3e36ce8b1c967145043de51ea5f90461))
* **progression:** finish the topic in progress before an earlier untouched one ([347c12a](https://github.com/Alisonka/english_agent/commit/347c12ab48598f0b9f84bb709d4fd7f08b064127))
* **srs:** keep the pre-rating card state in every review log ([f98c942](https://github.com/Alisonka/english_agent/commit/f98c94283f7e8529656496d0d9828bfb53124f6c))


### Changed

* **llm:** one tool loop and one budget meter for every agent ([b060e80](https://github.com/Alisonka/english_agent/commit/b060e801b0f709530232f56ca3a6dd4760ee10d1))
* **pipeline:** remove the legacy single-lesson writer ([dee2d11](https://github.com/Alisonka/english_agent/commit/dee2d1156da071bdb66505c3d5c0265abde2abda))

## [0.1.2](https://github.com/Alisonka/english_agent/compare/v0.1.1...v0.1.2) (2026-11-15)


### Added

* **generation:** planner, step writer and summary writer ([1e212f6](https://github.com/Alisonka/english_agent/commit/1e212f6665e9a9c2be1ff337c139babe4f689a98))
* **models:** topic plan, steps, summary and topic content ([8da7d50](https://github.com/Alisonka/english_agent/commit/8da7d5055abd0648dda7bf109a19b4afd7617165))
* **pipeline:** teach topics in steps, with cumulative practice and a cheat sheet ([366c719](https://github.com/Alisonka/english_agent/commit/366c719a9f5669b8635b8ab65e0afadad8687428))
* **practice:** cumulative quiz and translation selection from step banks ([401c2c5](https://github.com/Alisonka/english_agent/commit/401c2c55e88a3ce3be98dedffc210210e3a02033))
* **progression:** step ordering, re-teach, step rendering and topic weak points ([a55c03d](https://github.com/Alisonka/english_agent/commit/a55c03dd4bf330c301fcaec344158365cc31815e))
* **storage:** step columns, practice payload, progress and accuracy queries ([c92ead3](https://github.com/Alisonka/english_agent/commit/c92ead383369095be974b86c2eca02236e8b34aa))

## [0.1.1](https://github.com/Alisonka/english_agent/compare/v0.1.0...v0.1.1) (2026-11-13)


### Added

* **content:** write each lesson once per topic and level, share it with every learner ([aca8f20](https://github.com/Alisonka/english_agent/commit/aca8f202031ac86a4681c34617e355a81e903a82))
* **jobs:** TaskIQ worker and scheduler over Redis; bot, worker, scheduler services ([49546b8](https://github.com/Alisonka/english_agent/commit/49546b8a44435b4981eb0bf2a6a03c762351f918))
* **observability:** job traces, Prometheus metrics, admin alerts, load test ([00d45fa](https://github.com/Alisonka/english_agent/commit/00d45fa906c219b96639b405ea237ccc6ec59940))
* **schedule:** per-learner schedule and next-unsent-topic progression ([67b0862](https://github.com/Alisonka/english_agent/commit/67b0862ac572dffc71f9d1ff89a915814d687963))
* **storage:** learner model with core items, FSRS reviews and a vers… ([967ff8e](https://github.com/Alisonka/english_agent/commit/967ff8edede20e03ba4dbb8dbba1b062bea3f773))
* **storage:** learner model with core items, FSRS reviews and a versioned profile ([548a4cf](https://github.com/Alisonka/english_agent/commit/548a4cfe117f7d1bd4790378b547912c3bde141a))


### Changed

* **llm:** async Claude client, research, writer and grader ([a273b8e](https://github.com/Alisonka/english_agent/commit/a273b8e220e27b9ca931e49d6eed3fd43ebf0ba7))
* **runtime:** async storage and an aiogram bot on one event loop ([a70b1d8](https://github.com/Alisonka/english_agent/commit/a70b1d82c47174520b53ba2391ceed74e5f79c99))

## [0.1.0] - 2026-11-11

### Added

- Practice after the lesson. The writer now also returns `quiz` (3 Telegram quiz
  polls) and `translation` (3 RU->EN sentences) in the same call (lesson schema v3).
  The app sends the quiz `QUIZ_DELAY_MINUTES` after the lesson and the translation
  task at `TRANSLATION_SEND_TIME`, records poll answers and sends the result after
  the last one. A text reply to the translation task is graded by `GRADER_MODEL` at
  `GRADER_EFFORT` (`src/english_agent/grader.py`) and answered with corrections, an
  explanation and a "disagree" button. Wrong answers go to `errors`. Migration
  `0002_practice`: `practice_deliveries`, `quiz_polls`, `quiz_answers`,
  `translation_answers`. New settings: `QUIZ_DELAY_MINUTES`, `TRANSLATION_SEND_TIME`,
  `PRACTICE_GAP_MINUTES`, `PRACTICE_CUTOFF_TIME`, `GRADER_MODEL`, `GRADER_EFFORT`.
- PostgreSQL storage (`src/english_agent/storage/`): SQLAlchemy 2.0 (sync, psycopg 3)
  mappings and repositories that return pydantic records (`models/storage.py`), with
  Alembic migrations (`migrations/`, `0001_initial`: `learners`, `lessons`, `items`,
  `errors`, `cards`, `review_logs`). Sent and failed lessons are recorded, one per
  learner per day, so a restart or a manual run never sends a day twice. New
  settings: `DATABASE_URL`, `LESSON_SEND_TIME`, `TIMEZONE`.
- Server process `python -m english_agent.app`: the /level bot (long polling) and an
  APScheduler job that sends the daily lesson, with catch-up when started late.
  `Dockerfile` and compose services `postgres` (127.0.0.1 only), `migrate`, `app`.
- `python -m english_agent.storage import-learner-json` to move the old
  `.state/learner.json` level into the database.
- `scripts/pg_backup.sh` and systemd user units for a daily database backup.
- OpenTelemetry tracing (`src/english_agent/tracing.py`): one pipeline run becomes
  a trace of nested spans (`pipeline.run` -> `research.run` -> `llm.chat` /
  `tool.search_site` / `tool.fetch_page` / `tool.finish`, plus `writer.write_lesson`
  and `telegram.send_lesson`), with LLM model and token-usage attributes and HTTP
  child spans via `opentelemetry-instrumentation-httpx`. Each `llm.chat` span also
  records the model's own output (research notes and the lesson) as a
  `gen_ai.content.completion` event and an estimated `gen_ai.usage.cost_usd` derived
  from token counts via a local price table (`src/english_agent/llm/pricing.py`);
  prompts and fetched page text are never recorded, and the Telegram bot token is
  scrubbed from captured URLs. Tracing is
  opt-in: it is a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so CI and normal
  runs are unaffected. A `docker-compose.yml` runs Jaeger locally for viewing traces
  (OTLP/HTTP on `:4318`, UI on `:16686`). New setting: `OTEL_EXPORTER_OTLP_ENDPOINT`.

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

- `LLM_MAX_TOKENS` default raised from 4096 to 8192: the writer now also writes the
  practice, and adaptive thinking counts against the limit.
- `Lesson` schema reshaped for readable messenger delivery (schema version 1 -> 2):
  new required `key_idea_ru` (one-sentence core idea, shown first and never hidden),
  `rule_ru` shortened (max 1200) and written as short labelled paragraphs, and
  `nuances_ru` trimmed to 3-4 short items (max 200 chars each). The
  `write_lesson_system` prompt was updated to match.
- Formatted the codebase with `ruff format` and fixed lint / type findings.

- The learner's level is stored in the `learners` table instead of
  `.state/learner.json` (`LEARNER_STATE_PATH` is gone); sending a lesson now needs
  `DATABASE_URL` (or `--no-store`).
- Jaeger ports are published on 127.0.0.1 only.

### Deprecated

### Removed

- `.github/workflows/daily-lesson.yml`: the daily lesson is sent by the server app.

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
