from datetime import date, time
from functools import lru_cache
from pathlib import Path
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
    llm_max_tokens: int = 4096

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

    # Day the course starts: topic N (by `order`) is sent on start + N - 1 days.
    curriculum_start_date: date | None = None

    # Learner's CEFR level: picks which curriculum/<level>_topics.yaml is taught.
    # Default for a new learner only: once the learner picks a level in the bot, the
    # choice stored in the `learners` table wins.
    learner_cefr: Cefr = "A2"

    # PostgreSQL, e.g. postgresql+psycopg://english_agent:<pw>@localhost:5432/english_agent.
    database_url: SecretStr = Field(default=SecretStr(""))

    # When the long-running app (`python -m english_agent.app`) sends the daily lesson.
    lesson_send_time: time = time(9, 0)
    timezone: str = "Europe/Moscow"

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
