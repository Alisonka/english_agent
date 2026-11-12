from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator  # SecretStr прячет значение при печати/логировании
from pydantic_settings import BaseSettings, SettingsConfigDict

from english_agent.models.cefr import Cefr


class Settings(BaseSettings):
    """Runtime configuration. Values come from env vars or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    # Claude API model ids. Haiku 5.5 supports forced tool_choice (needed by the writer).
    research_model: str = "claude-haiku-5-5"
    writer_model: str = "claude-haiku-5-5"
    llm_max_tokens: int = 8192

    cache_dir: Path = Path(".cache")
    sitemap_ttl_days: int = 7  # сколько дней карта сайта (sitemap) считается «свежей» до повторной загрузки
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "english-agent/0.1"

    research_max_steps: int = 12
    research_max_fetches_per_site: int = 2
    research_page_char_limit: int = 40_000  # grammar articles on these sites run 15-35k chars

    # Telegram delivery. Token from @BotFather; chat id of the user the bot should write to
    # (`uv run python -m english_agent.telegram chats` lists the chats that have messaged the bot).
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"  # a fake Bot API in load tests
    # Webhook mode (off when empty): the public HTTPS URL Telegram posts updates to.
    telegram_webhook_url: str = ""
    telegram_webhook_secret: SecretStr = Field(default=SecretStr(""))
    # Outgoing rate limits, below Telegram's ~30 msg/s per bot and 1 msg/s per chat.
    telegram_global_rate_per_second: float = Field(default=25.0, gt=0)
    telegram_chat_rate_per_second: float = Field(default=1.0, gt=0)
    admin_chat_id: str = ""  # where operational alerts go; empty = alerts are only logged

    # Day the course starts: topic N (by `order`) is sent on start + N - 1 days.
    curriculum_start_date: date | None = None

    # Learner's CEFR level: picks which curriculum/<level>_topics.yaml is taught.
    # Default for a new learner only: once the learner picks a level in the bot, the
    # choice stored in the `learners` table wins.
    learner_cefr: Cefr = "A2"

    # PostgreSQL, e.g. postgresql+psycopg://english_agent:<pw>@localhost:5432/english_agent.
    database_url: SecretStr = Field(default=SecretStr(""))
    # Connections per process (bot, worker): pool_size kept open, max_overflow more under load.
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)

    # Redis: the job queue, de-duplication keys, the shared Telegram rate limit, the sitemap cache.
    # e.g. redis://localhost:6379/0. Holds no state that is not also in PostgreSQL.
    redis_url: SecretStr = Field(default=SecretStr(""))

    # When the long-running app (`python -m english_agent.app`) sends the daily lesson.
    lesson_send_time: time = time(9, 0)
    timezone: str = "Europe/Moscow"
    # A lesson not sent within this many hours after its send time is not sent that day.
    lesson_catch_up_hours: int = Field(default=6, ge=1, le=23)

    # Practice after the lesson: the quiz QUIZ_DELAY_MINUTES after the lesson, the translation
    # task at TRANSLATION_SEND_TIME but at least PRACTICE_GAP_MINUTES after the quiz; nothing
    # is sent after PRACTICE_CUTOFF_TIME (local time).
    quiz_delay_minutes: int = Field(default=60, ge=0)
    translation_send_time: time = time(18, 0)
    practice_gap_minutes: int = Field(default=60, ge=0)
    practice_cutoff_time: time = time(22, 0)

    # Spaced repetition (FSRS): the recall probability reviews aim for, and the longest gap between them.
    fsrs_desired_retention: float = Field(default=0.9, gt=0.5, lt=1.0)
    fsrs_maximum_interval_days: int = Field(default=180, ge=1)

    # Grades the learner's translation; effort is the Claude API `output_config.effort`.
    grader_model: str = "claude-haiku-5-5"
    grader_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # Claude API client: per-request timeout, SDK retries (429/5xx/overloaded, with backoff), and
    # how many requests one process keeps in flight (the account's rate limit is shared by all).
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=4, ge=0)
    llm_max_concurrency: int = Field(default=3, ge=1)

    # Background jobs: how often a failing job is retried, and when a claimed piece of work
    # (a lesson being sent, a lesson being written) is considered abandoned by a dead worker.
    job_max_attempts: int = Field(default=5, ge=1)
    lesson_claim_stale_minutes: int = Field(default=10, ge=1)
    content_generation_stale_minutes: int = Field(default=20, ge=1)

    log_format: Literal["text", "json"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # OpenTelemetry: OTLP/HTTP base endpoint (e.g. http://localhost:4318 for local
    # Jaeger). Empty = tracing disabled; spans become no-ops. Uses the standard
    # OTEL_EXPORTER_OTLP_ENDPOINT env var name.
    otel_exporter_otlp_endpoint: str = ""

    @field_validator("curriculum_start_date", mode="before")
    @classmethod
    def empty_date_is_none(cls, value: object) -> object:
        # GitHub Actions passes an unset variable as an empty string.
        return value or None

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone {value!r}") from error
        return value

    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value()) and bool(self.telegram_chat_id)


@lru_cache(maxsize=2)  # кэширует (запоминает) результаты вызова функции
def get_settings() -> Settings:
    return Settings()


# Каждый вызов Settings() заново читает .env и переменные окружения и собирает объект.
# Если get_settings() вызывается в нескольких местах, это повторная лишняя работа.
# dependency injection (передача зависимостей): настройки создаются один раз
# «на краю» программы и прокидываются внутрь.
