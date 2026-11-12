"""Topic -> research -> lesson -> Telegram. The one place that wires the steps together.

The server app calls `run_lesson_tick` every minute: every learner whose local send time has
come gets the next topic of their level they have not had yet. From the command line:

uv run python -m english_agent.pipeline                                 # TELEGRAM_CHAT_ID's next lesson, send
uv run python -m english_agent.pipeline --topic there_is_are            # a specific topic, send
uv run python -m english_agent.pipeline --date 2026-11-05 --no-send     # topic of that course day, print only

A sent lesson is recorded in the `lessons` table (one per learner per day); a run for a
day that already has a sent lesson does nothing. `--no-send` never touches the database.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from anthropic import AsyncAnthropic
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from english_agent.config import Settings, get_settings
from english_agent.content import content_key, worker_id
from english_agent.items import record_core_item
from english_agent.llm import AnthropicLLM, make_client
from english_agent.logs import bind, configure_logging
from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum, CurriculumTopic, load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.schedule import in_daily_window, local_now
from english_agent.models.storage import LearnerRecord, LessonContentRecord, LessonStatus
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import FileSitemapCache, SitemapCache, SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.storage.repositories import Stores, open_stores
from english_agent.telegram import TelegramClient, TelegramError, TelegramForbiddenError
from english_agent.tracing import configure_tracing, get_tracer
from english_agent.writer import write_lesson

COURSE_FINISHED = "Курс завершён: все темы пройдены."
LESSON_FAILED = "Урок «{title}» сегодня не получился. Пришлю его завтра."

# Exit codes of one lesson run.
OK, FAILED, USAGE, RETRY_LATER = 0, 1, 2, 75  # 75: EX_TEMPFAIL, the content is being written elsewhere

log = logging.getLogger(__name__)


def _http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


def _research_http(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


async def generate_lesson(
    topic: CurriculumTopic,
    settings: Settings,
    *,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
    client: AsyncAnthropic | None = None,
    sitemaps: SitemapCache | None = None,
) -> tuple[ResearchResult, Lesson | None]:
    """Returns the research result and, if a page was found, the lesson built from it.

    `client` and `sitemaps` are the process's shared Claude client and sitemap cache; without
    them a client is opened (and closed) for this call and sitemaps are cached on disk.
    """
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(_research_http(settings))
        if client is None:
            client = await stack.enter_async_context(make_client(settings))
        cache = sitemaps or FileSitemapCache(settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
        tools = LiveResearchTools(http, SitemapIndex(http, cache), page_char_limit=settings.research_page_char_limit)
        researcher = ResearchAgent(
            AnthropicLLM(settings, model=settings.research_model, client=client),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
            curriculum_version=curriculum_version,
            cefr=cefr,
        )
        research = await researcher.run(topic)
        if research.notes is None:
            return research, None
        lesson = await write_lesson(
            AnthropicLLM(settings, model=settings.writer_model, client=client),
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
    """Today in the default TIMEZONE; for command-line runs (a learner's day uses their own zone)."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


def deliver_lesson(lesson: Lesson, settings: Settings, chat_id: str) -> list[int]:
    """Send the lesson to the chat; returns the message ids."""
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN must be set")
    with _http_client(settings) as http:
        return TelegramClient(http, token).send_lesson(chat_id, lesson)


def topic_for_day(curriculum: Curriculum, day: int) -> CurriculumTopic | None:
    """Topic for 0-based course day `day`, or None once every topic has been sent (command-line previews)."""
    if day < 0:
        raise ValueError("day must be non-negative")
    topics = sorted(curriculum.topics, key=lambda topic: topic.order)
    return topics[day] if day < len(topics) else None


def next_topic(curriculum: Curriculum, sent_topic_ids: set[str]) -> CurriculumTopic | None:
    """The first topic, by order, the learner has not had yet; None once every topic is done.

    A failed or skipped day therefore never loses a topic, and a pause just resumes where it was.
    """
    ordered = sorted(curriculum.topics, key=lambda topic: topic.order)
    return next((topic for topic in ordered if topic.id not in sent_topic_ids), None)


def notify(settings: Settings, chat_id: str, text: str) -> None:
    """Send a short plain-text notice to the chat; a delivery failure is only logged."""
    try:
        with _http_client(settings) as http:
            client = TelegramClient(http, settings.telegram_bot_token.get_secret_value())
            client.send_message(chat_id, html.escape(text, quote=False))
    except TelegramError as error:
        log.warning("telegram error: %s", error)


def lesson_due(learner: LearnerRecord, today_status: LessonStatus | None, now: datetime, settings: Settings) -> bool:
    """Whether the learner's lesson should be sent at `now`.

    Inside [send time, send time + LESSON_CATCH_UP_HOURS) of the learner's local day, once the
    course has started, while the learner is active and not paused, and only if today has no
    row yet: a failed day is not retried every minute (the topic comes again tomorrow).
    """
    if not learner.receives_lessons or learner.curriculum_started_on is None or today_status is not None:
        return False
    local = local_now(learner.timezone, now)
    if local.date() < learner.curriculum_started_on:
        return False
    return in_daily_window(local, learner.lesson_send_time, settings.lesson_catch_up_hours)


def run_lesson_tick(settings: Settings, stores: Stores, now: datetime) -> int:
    """Run the lesson of every learner it is due for at `now`; returns how many runs ended without error."""
    learners = stores.learners.list_active()
    days = {learner.id: local_now(learner.timezone, now).date() for learner in learners}
    statuses = stores.lessons.statuses([(learner.id, days[learner.id]) for learner in learners])
    succeeded = 0
    for learner in learners:
        day = days[learner.id]
        if not lesson_due(learner, statuses.get((learner.id, day)), now, settings):
            continue
        with bind(chat_id=learner.telegram_chat_id):
            try:
                succeeded += run_lesson_for_learner(settings, stores, learner, day, now=now) == OK
            except Exception:  # one learner's failure must not stop the others
                log.exception("lesson run failed")
    return succeeded


def ensure_content(
    settings: Settings,
    stores: Stores,
    topic: CurriculumTopic,
    cefr: Cefr,
    curriculum_version: int,
    *,
    now: datetime | None = None,
) -> LessonContentRecord | None:
    """The shared lesson for the topic and level: reused if written, else written now by this caller.

    Returns a "ready" record, a "failed" one (this attempt failed, or every attempt did), or None
    when another worker is writing it or the last attempt failed only moments ago: try again later.
    """
    outcome, record = stores.contents.claim(
        topic.id,
        cefr,
        curriculum_version,
        content_key(settings),
        owner=worker_id(),
        now=now or datetime.now(UTC),
        stale_after=timedelta(minutes=settings.content_generation_stale_minutes),
        max_attempts=settings.job_max_attempts,
    )
    span = trace.get_current_span()
    span.set_attribute("content.id", record.id)
    span.set_attribute("content.outcome", outcome)
    if outcome in ("ready", "exhausted"):
        return record
    if outcome == "busy":
        return None
    log.info("writing the lesson for %s (%s), attempt %d", topic.id, cefr, record.attempts)
    try:
        # Bridge until the stores are async: one event loop for this lesson's research and writing.
        research, lesson = asyncio.run(
            generate_lesson(topic, settings, cefr=cefr, curriculum_version=curriculum_version)
        )
        reason = research.message
    except Exception as error:  # unattended run: every failure is recorded, not just logged
        lesson, reason = None, f"{type(error).__name__}: {error}"
    if lesson is None:
        return stores.contents.fail(record.id, reason)
    return stores.contents.finish(record.id, lesson)


def run_lesson_for_learner(
    settings: Settings,
    stores: Stores,
    learner: LearnerRecord,
    today: date,
    *,
    topic_id: str | None = None,
    cefr: Cefr | None = None,
    send: bool = True,
    now: datetime | None = None,
) -> int:
    """Send the learner's next lesson (or `topic_id`) and record it; returns an exit code.

    The lesson itself is shared content (see `ensure_content`): written by the first learner's run
    to reach the topic at this level, reused by everyone after.
    """
    with get_tracer().start_as_current_span("pipeline.run") as span:
        span.set_attribute("pipeline.send", send)
        level = cefr or learner.cefr
        span.set_attribute("lesson.cefr", level)
        curriculum = load_curriculum(level)
        if topic_id:
            topic = next((item for item in curriculum.topics if item.id == topic_id), None)
            if topic is None:
                log.error("unknown topic %r", topic_id)
                return USAGE
        else:
            done = stores.lessons.sent_topic_ids(learner.id, level)
            skipped = stores.contents.unavailable_topics(
                level, curriculum.version, content_key(settings), max_attempts=settings.job_max_attempts
            )
            topic = next_topic(curriculum, done | skipped)
            if topic is None:
                log.info("course finished at level %s", level)
                if send:
                    stores.learners.deactivate(learner.telegram_chat_id, "course_finished")
                    notify(settings, learner.telegram_chat_id, COURSE_FINISHED)
                return OK
        span.set_attribute("topic.id", topic.id)

        if stores.lessons.sent_on(learner.id, today):
            log.info("lesson for %s is already sent", today.isoformat())
            return OK

        def record_failed(reason: str) -> None:
            stores.lessons.record_failed(
                learner.id, today, topic_id=topic.id, cefr=level, reason=reason, curriculum_version=curriculum.version
            )

        content = ensure_content(settings, stores, topic, level, curriculum.version, now=now)
        if content is None:
            log.info("the lesson for %s is being written elsewhere; next tick", topic.id)
            return RETRY_LATER
        lesson = content.lesson
        if content.status != "ready" or lesson is None:
            reason = content.error or "the lesson could not be written"
            log.error("lesson for %s failed: %s", topic.id, reason)
            span.set_status(Status(StatusCode.ERROR, reason))
            record_failed(reason)
            if send:
                notify(settings, learner.telegram_chat_id, LESSON_FAILED.format(title=topic.title_ru))
            return FAILED
        if not send:
            print(lesson.model_dump_json(indent=2, ensure_ascii=False))
            return OK

        try:
            message_ids = deliver_lesson(lesson, settings, learner.telegram_chat_id)
        except TelegramError as error:
            log.error("telegram error: %s", error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            record_failed(f"telegram: {error}")
            if isinstance(error, TelegramForbiddenError):
                stores.learners.deactivate(learner.telegram_chat_id, "blocked")
            return FAILED
        log.info("sent %d message(s) to Telegram chat %s", len(message_ids), learner.telegram_chat_id)
        record = stores.lessons.record_sent(
            learner.id, today, lesson, message_ids, curriculum_version=curriculum.version, content_id=content.id
        )
        record_core_item(stores.items, lesson, record.id)
        return OK


def preview_lesson(
    settings: Settings,
    today: date,
    *,
    topic_id: str | None,
    level: Cefr,
    send_to: str | None = None,
) -> int:
    """Write (and print) a lesson without the database: `topic_id`, or the topic of course day `today`."""
    curriculum = load_curriculum(level)
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
            return 0
    try:
        research, lesson = asyncio.run(
            generate_lesson(topic, settings, cefr=level, curriculum_version=curriculum.version)
        )
        reason = research.message
    except Exception as error:
        lesson, reason = None, f"{type(error).__name__}: {error}"
    if lesson is None:
        log.error("lesson for %s failed: %s", topic.id, reason)
        return 1
    print(lesson.model_dump_json(indent=2, ensure_ascii=False))
    if send_to is None:
        return 0
    try:
        message_ids = deliver_lesson(lesson, settings, send_to)
    except TelegramError as error:
        log.error("telegram error: %s", error)
        return 1
    log.info("sent %d message(s) to Telegram chat %s", len(message_ids), send_to)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research a topic, write the lesson and send it to Telegram.")
    parser.add_argument("--topic", help="topic id from the level's curriculum file (default: the learner's next one)")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="the lesson's day, YYYY-MM-DD (default: today); with --no-send, picks the topic of that course day",
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
    parser.add_argument("--chat", help="the learner's chat id (default: TELEGRAM_CHAT_ID)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    send = not args.no_send
    chat_id = args.chat or settings.telegram_chat_id
    # Fail before spending LLM calls on a lesson we could not deliver.
    if send and not (settings.telegram_bot_token.get_secret_value() and chat_id):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or --chat) must be set (or pass --no-send)")
        return 2
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
        if stores is None:
            today = args.date or local_today(settings)
            level = cast(Cefr, args.level or settings.learner_cefr)
            return preview_lesson(settings, today, topic_id=args.topic, level=level, send_to=chat_id if send else None)
        learner = stores.learners.get_or_create(chat_id, settings.learner_cefr, settings.default_schedule())
        today = args.date or local_now(learner.timezone, datetime.now(UTC)).date()
        cefr = resolve_cefr(args.level, learner, settings.learner_cefr)
        with bind(chat_id=chat_id):
            return run_lesson_for_learner(settings, stores, learner, today, topic_id=args.topic, cefr=cefr)
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
