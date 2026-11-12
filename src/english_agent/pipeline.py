"""Topic -> research -> lesson -> Telegram. The one place that wires the steps together.

uv run python -m english_agent.pipeline                                 # topic of the day, send
uv run python -m english_agent.pipeline --date 2026-11-05 --no-send     # topic of that day, print only
uv run python -m english_agent.pipeline --topic there_is_are            # a specific topic, send

A sent lesson is recorded in the `lessons` table (one per learner per day); a run for a
day that already has a sent lesson does nothing. `--no-send` never touches the database.
"""

from __future__ import annotations

import argparse
import html
import logging
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from opentelemetry.trace import Span, Status, StatusCode

from english_agent.config import Settings, get_settings
from english_agent.items import record_core_item
from english_agent.llm import AnthropicLLM
from english_agent.logs import configure_logging
from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum, CurriculumTopic, load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.storage.repositories import Stores, open_stores
from english_agent.telegram import TelegramClient, TelegramError
from english_agent.tracing import configure_tracing, get_tracer
from english_agent.writer import write_lesson

COURSE_FINISHED = "Курс завершён: все темы пройдены."

log = logging.getLogger(__name__)


def _http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


def generate_lesson(
    topic: CurriculumTopic, settings: Settings, *, cefr: Cefr = "A2", curriculum_version: int | None = None
) -> tuple[ResearchResult, Lesson | None]:
    """Returns the research result and, if a page was found, the lesson built from it."""
    with _http_client(settings) as http:
        index = SitemapIndex(http, settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
        tools = LiveResearchTools(http, index, page_char_limit=settings.research_page_char_limit)
        researcher = ResearchAgent(
            AnthropicLLM(settings, model=settings.research_model),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
            curriculum_version=curriculum_version,
            cefr=cefr,
        )
        research = researcher.run(topic)

    if research.notes is None:
        return research, None
    lesson = write_lesson(
        AnthropicLLM(settings, model=settings.writer_model),
        topic,
        research.notes,
        cefr=cefr,
        curriculum_version=curriculum_version,
    )
    return research, lesson


def resolve_cefr(explicit: str | None, learner: LearnerRecord | None, default: Cefr) -> Cefr:
    """The level to teach: an explicit --level, else the learner's saved choice, else the default."""
    if explicit is not None:
        return cast(Cefr, explicit)
    return learner.cefr if learner is not None else default


def local_today(settings: Settings) -> date:
    """Today's date in the configured TIMEZONE (the server itself runs in UTC)."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def deliver_lesson(lesson: Lesson, settings: Settings) -> list[int]:
    """Send the lesson to the configured Telegram chat; returns the message ids."""
    if not settings.telegram_configured():
        raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    with _http_client(settings) as http:
        client = TelegramClient(http, settings.telegram_bot_token.get_secret_value())
        return client.send_lesson(settings.telegram_chat_id, lesson)


def topic_for_day(curriculum: Curriculum, day: int) -> CurriculumTopic | None:
    """Topic for 0-based course day `day`, or None once every topic has been sent."""
    if day < 0:
        raise ValueError("day must be non-negative")
    topics = sorted(curriculum.topics, key=lambda topic: topic.order)
    return topics[day] if day < len(topics) else None


def notify(settings: Settings, text: str) -> None:
    """Send a short plain-text notice to the chat; a delivery failure is only logged."""
    try:
        with _http_client(settings) as http:
            client = TelegramClient(http, settings.telegram_bot_token.get_secret_value())
            client.send_message(settings.telegram_chat_id, html.escape(text, quote=False))
    except TelegramError as error:
        log.warning("telegram error: %s", error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research a topic, write the lesson and send it to Telegram.")
    parser.add_argument("--topic", help="topic id from the level's curriculum file (default: topic of the day)")
    parser.add_argument(
        "--date", type=date.fromisoformat, help="pick the topic of this day, YYYY-MM-DD (default: today)"
    )
    parser.add_argument("--no-send", action="store_true", help="print the lesson but do not send it to Telegram")
    parser.add_argument(
        "--no-store", action="store_true", help="do not read or record lessons in the database (implied by --no-send)"
    )
    parser.add_argument(
        "--level",
        choices=list(CEFR_LEVELS),
        help="CEFR level to teach (default: the learner's saved choice, else LEARNER_CEFR)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    send = not args.no_send
    stores: Stores | None = None
    if send and not args.no_store:
        stores = open_stores(settings)
        if stores is None:
            log.error("DATABASE_URL must be set (or pass --no-store)")
            return 2

    # Tracing is a no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set; shutdown() flushes
    # the batched spans, which matters because this process exits right after main().
    provider = configure_tracing(settings)
    try:
        today = args.date or local_today(settings)
        return run_daily(settings, stores, today, topic_id=args.topic, level=args.level, send=send)
    finally:
        if provider is not None:
            provider.shutdown()


def run_daily(
    settings: Settings,
    stores: Stores | None,
    today: date,
    *,
    topic_id: str | None = None,
    level: str | None = None,
    send: bool = True,
) -> int:
    """Pick the day's topic, write the lesson, send it and record it; returns an exit code.

    Shared by the CLI and the scheduler in `english_agent.app`. With `stores=None` nothing
    is read from or written to the database.
    """
    with get_tracer().start_as_current_span("pipeline.run") as span:
        span.set_attribute("pipeline.send", send)
        learner = None
        if stores is not None and settings.telegram_chat_id:
            learner = stores.learners.get(settings.telegram_chat_id)
        cefr = resolve_cefr(level, learner, settings.learner_cefr)
        span.set_attribute("lesson.cefr", cefr)
        curriculum = load_curriculum(cefr)
        return _run(settings, stores, learner, curriculum, today, topic_id, send, span, cefr)


def _run(
    settings: Settings,
    stores: Stores | None,
    learner: LearnerRecord | None,
    curriculum: Curriculum,
    today: date,
    topic_id: str | None,
    send: bool,
    span: Span,
    cefr: Cefr,
) -> int:
    if topic_id:
        topic = next((item for item in curriculum.topics if item.id == topic_id), None)
        if topic is None:
            log.error("unknown topic %r", topic_id)
            return 2
    else:
        start = settings.curriculum_start_date
        if start is None:
            log.error("CURRICULUM_START_DATE must be set (or pass --topic)")
            return 2
        day = (today - start).days
        if day < 0:
            print(f"course starts on {start.isoformat()}")
            return 0
        topic = topic_for_day(curriculum, day)
        if topic is None:
            print(COURSE_FINISHED)
            # Only on the first day after the last topic, so a still-running schedule does not repeat it.
            if send and day == len(curriculum.topics) and settings.telegram_configured():
                notify(settings, COURSE_FINISHED)
            return 0

    span.set_attribute("topic.id", topic.id)

    # Fail before spending LLM calls on a lesson we could not deliver.
    if send and not settings.telegram_configured():
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or pass --no-send)")
        return 2

    if stores is not None:
        learner = learner or stores.learners.get_or_create(settings.telegram_chat_id, settings.learner_cefr)
        if stores.lessons.sent_on(learner.id, today):
            log.info("lesson for %s is already sent", today.isoformat())
            return 0

    def record_failed(reason: str) -> None:
        if stores is not None and learner is not None:
            stores.lessons.record_failed(
                learner.id, today, topic_id=topic.id, cefr=cefr, reason=reason, curriculum_version=curriculum.version
            )

    lesson: Lesson | None = None
    try:
        research, lesson = generate_lesson(topic, settings, cefr=cefr, curriculum_version=curriculum.version)
        reason = research.message
    except Exception as error:  # unattended run: every failure has to reach the chat, not just the log
        reason = f"{type(error).__name__}: {error}"
    if lesson is None:
        log.error("lesson for %s failed: %s", topic.id, reason)
        span.set_status(Status(StatusCode.ERROR, reason))
        record_failed(reason)
        if send:
            notify(settings, f"Урок «{topic.title_ru}» сегодня не получился: {reason}")
        return 1
    print(lesson.model_dump_json(indent=2, ensure_ascii=False))
    if not send:
        return 0

    try:
        message_ids = deliver_lesson(lesson, settings)
    except TelegramError as error:
        log.error("telegram error: %s", error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        record_failed(f"telegram: {error}")
        return 1
    log.info("sent %d message(s) to Telegram chat %s", len(message_ids), settings.telegram_chat_id)
    if stores is not None and learner is not None:
        record = stores.lessons.record_sent(
            learner.id, today, lesson, message_ids, curriculum_version=curriculum.version
        )
        record_core_item(stores.items, lesson, record.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
