# english-agent

Daily A2 English grammar lessons, researched from curated Russian-language
learning sites and written up by an LLM. The project researches a grammar topic
against live sources, then produces a structured, validated lesson.

## How it works

```mermaid
flowchart LR
    topic["Curriculum topic (curriculum/*.yaml)"] --> research[ResearchAgent]
    research -->|"reads live sites via sitemaps"| sources[("englex.ru / skyeng.ru / skysmart.ru")]
    research --> notes[TopicNotes]
    notes --> writer[write_lesson]
    writer -->|"one forced tool call (Claude)"| lesson["Validated Lesson (JSON)"]
    lesson --> telegram["Telegram (HTML message)"]
```

- **Research** ([src/english_agent/research/](src/english_agent/research/)): an
  agent crawls sitemaps and reads grammar articles from three curated sources
  (`englex.ru`, `skyeng.ru`, `skysmart.ru`), returning structured notes.
- **Writer** ([src/english_agent/writer.py](src/english_agent/writer.py)): turns
  the notes into a validated `Lesson`. Provenance fields (`topic_id`, `cefr`,
  `sources`) are owned by code, so a lesson can never cite a page the research
  step did not read.
- **Telegram** ([src/english_agent/telegram/](src/english_agent/telegram/)):
  renders a `Lesson` as Telegram HTML (split into several messages if it exceeds
  the 4096-character limit) and sends it through the Bot API.
- **Pipeline** ([src/english_agent/pipeline.py](src/english_agent/pipeline.py)):
  wires topic -> research -> lesson -> Telegram together.

## Reproducibility (provenance)

Every `Lesson` and `ResearchResult` carries a `provenance` passport so you can
trace exactly how it was produced: the pipeline version, the LLM model id, the
content fingerprints of the prompts used, the data schema version, and the
curriculum version. Component versions live in one place,
[src/english_agent/versions.py](src/english_agent/versions.py). Bump a
`SCHEMA_VERSIONS` entry when a model's shape changes; prompt fingerprints update
automatically from the prompt files.

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) for dependency management
- An Anthropic API key (Claude)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) to receive lessons

## Setup

```bash
# 1. Install dependencies into a local .venv
uv sync

# 2. Create your local env file and fill in the API key and bot token
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=... and TELEGRAM_BOT_TOKEN=...

# 3. Open your bot in Telegram and send it /start (bots cannot message you first),
#    then find your chat id and put it into .env as TELEGRAM_CHAT_ID
uv run python -m english_agent.telegram chats

# 4. Check delivery
uv run python -m english_agent.telegram ping
```

Configuration is read from environment variables or a local `.env` file (see
[src/english_agent/config.py](src/english_agent/config.py)). Never commit `.env`.

### Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | – | Anthropic (Claude) API key |
| `TELEGRAM_BOT_TOKEN` | for sending | – | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | for sending | – | Chat that receives the lessons (`python -m english_agent.telegram chats`) |
| `CURRICULUM_START_DATE` | without `--topic` | – | Day topic 1 is sent (`YYYY-MM-DD`); one topic per day after that |
| `RESEARCH_MODEL` | no | `claude-haiku-5-5` | Model used by the research agent |
| `WRITER_MODEL` | no | `claude-haiku-5-5` | Model used by the lesson writer |
| `LLM_MAX_TOKENS` | no | `4096` | Max tokens per LLM call |
| `RESEARCH_MAX_STEPS` | no | `12` | Max research agent steps |
| `RESEARCH_MAX_FETCHES_PER_SITE` | no | `2` | Max page fetches per site |
| `RESEARCH_PAGE_CHAR_LIMIT` | no | `40000` | Max chars read per page |
| `SITEMAP_TTL_DAYS` | no | `7` | Days a cached sitemap stays fresh |

See [.env.example](.env.example) for the full template.

## Usage

Topic ids come from [curriculum/a2_topics.yaml](curriculum/a2_topics.yaml).

Run the full pipeline (research + write a lesson + send it to Telegram):

```bash
uv run python -m english_agent.pipeline --topic there_is_are
```

Add `--no-send` to only print the lesson without sending it. Without `--topic`
the pipeline sends the topic of the day (counted from `CURRICULUM_START_DATE`;
`--date YYYY-MM-DD` checks another day). After the last topic it announces
"курс завершён" once and exits with 0; a failed lesson is reported to the chat.

### Daily schedule

[.github/workflows/daily-lesson.yml](.github/workflows/daily-lesson.yml) runs the
pipeline every day at 06:00 UTC (09:00 Moscow) and can be started by hand from
the Actions tab. It needs repository secrets `ANTHROPIC_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and the repository variable
`CURRICULUM_START_DATE`.

Run only the research step for one topic:

```bash
uv run python -m english_agent.research --topic present_perfect_vs_past_simple
```

Both commands print JSON to stdout.

## Development

```bash
# Run the test suite
uv run pytest

# Lint, format and type-check
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow
(branching, commit conventions, linting) and [CHANGELOG.md](CHANGELOG.md) for the
release history.

## Project layout

```
src/english_agent/
  config.py        # runtime settings (env / .env)
  paths.py         # repo-relative paths
  pipeline.py      # topic -> research -> lesson -> Telegram
  writer.py        # notes -> validated Lesson
  llm/             # Anthropic (Claude) client
  models/          # pydantic models (curriculum, lesson, golden)
  prompts/         # system prompts
  research/        # research agent, tools, sitemaps, sources
  telegram/        # Lesson -> HTML rendering, Bot API client, setup helpers
curriculum/        # A2 / B1 topic definitions
evals/datasets/    # evaluation goldens
tests/             # test suite
```

## License

Released under the [MIT License](LICENSE).
