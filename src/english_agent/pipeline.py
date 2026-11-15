"""Topic -> research -> lesson -> Telegram. The one place that wires the steps together.

The server calls `run_lesson_tick` every minute: every learner whose local send time has come
gets the next topic of their level they have not had yet, written once per topic and level
(see `ensure_content`) and shared by everyone who reaches it. From the command line:

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
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy.exc import SQLAlchemyError

from english_agent.alerts import Alerter
from english_agent.config import Settings, get_settings
from english_agent.content import content_key, worker_id
from english_agent.items import step_item_from
from english_agent.logs import bind, configure_logging
from english_agent.metrics import LESSONS_FAILED, LESSONS_SENT
from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum, CurriculumTopic, load_curriculum
from english_agent.models.schedule import local_now
from english_agent.models.storage import ItemAccuracy, LearnerRecord, LessonContentRecord
from english_agent.planning import lesson_due
from english_agent.profile import weak_points_for_topic
from english_agent.progression import NextStep, TopicProgress, is_done, next_step, reteach_candidate, topic_progress
from english_agent.research.sitemaps import SitemapCache
from english_agent.session_runner import run_topic_session
from english_agent.storage.repositories import Stores, open_stores
from english_agent.telegram.render import render_cheat_sheet, render_step
from english_agent.telegram.sender import Sender, TelegramError, TelegramForbiddenError, make_bot
from english_agent.tracing import configure_tracing, get_tracer

COURSE_FINISHED = "Курс завершён: все темы пройдены."
LESSON_FAILED = "Урок «{title}» сегодня не получился. Пришлю его завтра."

# Exit codes of one lesson run.
OK, FAILED, USAGE, RETRY_LATER = 0, 1, 2, 75  # 75: EX_TEMPFAIL, the content is being written elsewhere

_CONCURRENT_LESSONS = 16  # learners served at once by one tick; Claude and Telegram have their own limits

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    """What a lesson run needs: the process's stores, bot, Claude client and sitemap cache."""

    settings: Settings
    stores: Stores
    sender: Sender
    client: AsyncAnthropic | None = None  # None: each generation opens its own
    sitemaps: SitemapCache | None = None  # None: sitemaps cached on disk
    alerts: Alerter | None = None  # None: problems are only logged


def resolve_cefr(explicit: str | None, learner: LearnerRecord | None, default: Cefr) -> Cefr:
    """The level to teach: an explicit --level, else the learner's saved choice, else the default."""
    if explicit is not None:
        return cast(Cefr, explicit)
    return learner.cefr if learner is not None else default


def local_today(settings: Settings) -> date:
    """Today in the default TIMEZONE; for command-line runs (a learner's day uses their own zone)."""
    return datetime.now(ZoneInfo(settings.timezone)).date()


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


async def notify(sender: Sender, chat_id: str, text: str) -> None:
    """Send a short plain-text notice to the chat; a delivery failure is only logged."""
    try:
        await sender.send_text(chat_id, html.escape(text, quote=False))
    except TelegramError as error:
        log.warning("telegram error: %s", error)


async def run_lesson_tick(runtime: Runtime, now: datetime) -> int:
    """Run, in this process, the lesson of every learner it is due for at `now` (local runs and tests).

    The server does the same through the job queue: the scheduler enqueues one `deliver_lesson`
    job per due learner. Returns how many runs ended without error.
    """
    stores = runtime.stores
    learners = await stores.learners.list_active()
    days = {learner.id: local_now(learner.timezone, now).date() for learner in learners}
    states = await stores.lessons.states([(learner.id, days[learner.id]) for learner in learners])
    due = [
        learner
        for learner in learners
        if lesson_due(learner, states.get((learner.id, days[learner.id])), now, runtime.settings)
    ]
    gate = asyncio.Semaphore(_CONCURRENT_LESSONS)

    async def one(learner: LearnerRecord) -> bool:
        async with gate:
            with bind(chat_id=learner.telegram_chat_id):
                try:
                    return await run_lesson_for_learner(runtime, learner, days[learner.id], now=now) == OK
                except Exception:  # one learner's failure must not stop the others
                    log.exception("lesson run failed")
                    return False

    return sum(await asyncio.gather(*(one(learner) for learner in due)))


async def ensure_content(
    runtime: Runtime,
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
    settings, stores = runtime.settings, runtime.stores
    outcome, record = await stores.contents.claim(
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
    log.info("writing the topic %s (%s), attempt %d", topic.id, cefr, record.attempts)
    session = await run_topic_session(
        topic,
        settings,
        cefr=cefr,
        curriculum_version=curriculum_version,
        client=runtime.client,
        sitemaps=runtime.sitemaps,
    )
    log.info("topic %s (%s) run: %s", topic.id, cefr, session.report.summary())
    if session.content is None:
        reason = session.report.error or "the topic could not be written"
        return await stores.contents.fail(record.id, reason, run=session.report)
    return await stores.contents.finish(record.id, session.content, run=session.report)


async def run_lesson_for_learner(
    runtime: Runtime,
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
    settings, stores, sender = runtime.settings, runtime.stores, runtime.sender
    chat_id = learner.telegram_chat_id
    with get_tracer().start_as_current_span("pipeline.run") as span:
        span.set_attribute("pipeline.send", send)
        level = cefr or learner.cefr
        span.set_attribute("lesson.cefr", level)
        curriculum = load_curriculum(level)
        now = now or datetime.now(UTC)

        sent = await stores.lessons.sent_steps(learner.id, level)
        progress = topic_progress(sent)
        accuracy = await _step_accuracy(stores, learner.id, [step.item_key for step in sent])
        if topic_id:
            topic = next((item for item in curriculum.topics if item.id == topic_id), None)
            if topic is None:
                log.error("unknown topic %r", topic_id)
                return USAGE
            nxt = _next_in_topic(topic, progress.get(topic.id), accuracy)
        else:
            skipped = await stores.contents.unavailable_topics(
                level, curriculum.version, content_key(settings), max_attempts=settings.job_max_attempts
            )
            found = next_step(curriculum, progress, skipped, accuracy)
            if found is None:
                log.info("course finished at level %s", level)
                if send:
                    await _finish_course(runtime, learner, level, progress)
                    await stores.learners.deactivate(chat_id, "course_finished")
                    await notify(sender, chat_id, COURSE_FINISHED)
                return OK
            nxt, topic = found, found.topic
        span.set_attribute("topic.id", topic.id)
        span.set_attribute("step.index", nxt.index)
        span.set_attribute("step.variant", nxt.variant)

        claim, row = await stores.lessons.claim(
            learner.id,
            today,
            topic_id=topic.id,
            cefr=level,
            curriculum_version=curriculum.version,
            now=now,
            stale_after=timedelta(minutes=settings.lesson_claim_stale_minutes),
            max_attempts=settings.job_max_attempts,
        )
        span.set_attribute("lesson.claim", claim)
        if claim == "done":
            log.info("lesson for %s is already done (%s)", today.isoformat(), row.status)
            return OK
        if claim == "busy":
            return RETRY_LATER

        if nxt.content_id is not None:
            content = await stores.contents.get(nxt.content_id)
        else:
            content = await ensure_content(runtime, topic, level, curriculum.version, now=now)
        if content is None:
            log.info("the topic %s is being written elsewhere; next tick", topic.id)
            await stores.lessons.release(row.id)
            return RETRY_LATER
        if content.status != "ready" or content.topic is None:
            reason = content.error or "the topic could not be written"
            log.error("topic %s failed: %s", topic.id, reason)
            span.set_status(Status(StatusCode.ERROR, reason))
            await stores.lessons.fail(row.id, reason, final=True)  # the topic comes again tomorrow
            LESSONS_FAILED.labels("true").inc()
            if runtime.alerts is not None:
                run = f"\n{content.run.summary()}" if content.run is not None else ""
                await runtime.alerts.alert("topic not written", f"{topic.id} ({level}): {reason}{run}")
            if send:
                await notify(sender, chat_id, LESSON_FAILED.format(title=topic.title_ru))
            return FAILED

        topic_content = content.topic
        step = topic_content.step(nxt.index, nxt.variant)
        if not send:
            print(step.model_dump_json(indent=2, ensure_ascii=False))
            await stores.lessons.release(row.id)
            return OK

        # A step is one message, so a cut-short send just leaves the row without a message id.
        already_sent = bool(row.telegram_message_ids)
        if nxt.leaving is not None and not already_sent:
            await _send_cheat_sheet(runtime, learner, level, nxt.leaving)
        await stores.lessons.start_sending(row.id, content.id, step)
        try:
            if not already_sent:
                message_id = await sender.send_text(chat_id, render_step(step))
                await stores.lessons.add_message_id(row.id, message_id)
        except TelegramForbiddenError as error:
            log.info("chat blocked the bot, deactivating: %s", error)
            await stores.lessons.fail(row.id, f"telegram: {error}", final=True)
            await stores.learners.deactivate(chat_id, "blocked")
            return FAILED
        except TelegramError as error:
            log.error("telegram error: %s", error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            await stores.lessons.fail(row.id, f"telegram: {error}", final=False)  # retried after a backoff
            LESSONS_FAILED.labels("false").inc()
            return FAILED
        record = await stores.lessons.finish_sending(row.id)
        LESSONS_SENT.labels(level).inc()
        # The learner already has the step, so a failure recording its item is logged, never raised.
        try:
            await stores.items.get_or_create(
                step_item_from(step, topic_content.form_for(nxt.index)), source_lesson_id=record.id
            )
        except SQLAlchemyError as error:
            log.warning("could not record the item for %s: %s", step.item_key, error)
        return OK


async def _step_accuracy(stores: Stores, learner_id: int, item_keys: list[str]) -> dict[str, ItemAccuracy]:
    """Per-item accuracy keyed by item key, for the progression's re-teach decision."""
    id_by_key = await stores.items.ids_for_keys(item_keys)
    by_id = await stores.cards.item_accuracy(learner_id, list(id_by_key.values()))
    return {key: by_id[item_id] for key, item_id in id_by_key.items() if item_id in by_id}


def _next_in_topic(
    topic: CurriculumTopic, progress: TopicProgress | None, accuracy: dict[str, ItemAccuracy]
) -> NextStep:
    """The next step within one explicitly chosen topic (the --topic command-line override)."""
    if progress is None:
        return NextStep(topic, 1, "main", None, None)
    slug = reteach_candidate(progress, accuracy)
    if slug is not None:
        index = next(step.step_index for step in progress.sent if step.variant == "main" and step.step_slug == slug)
        return NextStep(topic, index, "alt", progress.content_id, None)
    sent_main = {step.step_index for step in progress.sent if step.variant == "main"}
    index = next((i for i in range(1, progress.step_count + 1) if i not in sent_main), 1)
    return NextStep(topic, index, "main", progress.content_id, None)


async def _send_cheat_sheet(runtime: Runtime, learner: LearnerRecord, level: Cefr, topic_id: str) -> None:
    """Send the finished topic's cheat sheet once, with the learner's weak points; failures are logged."""
    stores = runtime.stores
    try:
        rows = [step for step in await stores.lessons.sent_steps(learner.id, level) if step.topic_id == topic_id]
        if not rows or rows[0].content_id is None:
            return
        last = max(rows, key=lambda step: (step.lesson_date, step.lesson_id))
        delivery = await stores.practice.claim_delivery(last.lesson_id, "summary", 0)
        if delivery is None:  # already sent
            return
        content = await stores.contents.get(rows[0].content_id)
        if content is None or content.topic is None:
            return
        topic_content = content.topic
        accuracy = await _step_accuracy(stores, learner.id, topic_content.item_keys())
        weak = weak_points_for_topic(topic_content.steps, accuracy, {})
        message_ids = [
            await runtime.sender.send_text(learner.telegram_chat_id, message)
            for message in render_cheat_sheet(topic_content.summary, weak)
        ]
        await stores.practice.mark_sent(delivery.id, message_ids)
    except (TelegramError, SQLAlchemyError) as error:
        log.warning("could not send the cheat sheet for %s: %s", topic_id, error)


async def _finish_course(
    runtime: Runtime, learner: LearnerRecord, level: Cefr, progress: dict[str, TopicProgress]
) -> None:
    """At the end of the course, send the last finished topic's cheat sheet before deactivating."""
    done = [topic_id for topic_id, prog in progress.items() if is_done(prog)]
    if done:
        last = max(done, key=lambda topic_id: max(step.lesson_date for step in progress[topic_id].sent))
        await _send_cheat_sheet(runtime, learner, level, last)


async def preview_topic(
    settings: Settings,
    today: date,
    *,
    topic_id: str | None,
    level: Cefr,
    sender: Sender | None = None,
    chat_id: str | None = None,
) -> int:
    """Write (and print) a topic without the database: `topic_id`, or the topic of course day `today`.

    With a `sender` (a send run with `--no-store`) every step and the cheat sheet go to the chat at
    once, which shows the whole topic as a learner would get it over several days.
    """
    curriculum = load_curriculum(level)
    if topic_id:
        topic = next((item for item in curriculum.topics if item.id == topic_id), None)
        if topic is None:
            log.error("unknown topic %r", topic_id)
            return USAGE
    else:
        start = settings.curriculum_start_date
        if start is None:
            log.error("CURRICULUM_START_DATE must be set (or pass --topic)")
            return USAGE
        day = (today - start).days
        if day < 0:
            print(f"course starts on {start.isoformat()}")
            return OK
        topic = topic_for_day(curriculum, day)
        if topic is None:
            print(COURSE_FINISHED)
            return OK
    session = await run_topic_session(topic, settings, cefr=level, curriculum_version=curriculum.version)
    log.info("topic %s run: %s", topic.id, session.report.summary())
    content = session.content
    if content is None:
        log.error("topic %s failed: %s", topic.id, session.report.error)
        return FAILED
    print(content.model_dump_json(indent=2, ensure_ascii=False))
    if sender is None or chat_id is None:
        return OK
    messages = [render_step(step) for step in content.steps] + render_cheat_sheet(content.summary, [])
    try:
        for message in messages:
            await sender.send_text(chat_id, message)
    except TelegramError as error:
        log.error("telegram error: %s", error)
        return FAILED
    log.info("sent %d message(s) to Telegram chat %s", len(messages), chat_id)
    return OK


@asynccontextmanager
async def open_sender(settings: Settings) -> AsyncIterator[Sender]:
    """A bot for one command-line run, closed afterwards."""
    bot = make_bot(settings)
    try:
        yield Sender(bot)
    finally:
        await bot.session.close()


def _parser() -> argparse.ArgumentParser:
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
    return parser


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    send = not args.no_send
    chat_id = args.chat or settings.telegram_chat_id
    # Fail before spending LLM calls on a lesson we could not deliver.
    if send and not (settings.telegram_bot_token.get_secret_value() and chat_id):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or --chat) must be set (or pass --no-send)")
        return USAGE
    async with AsyncExitStack() as stack:
        stores: Stores | None = None
        if send and not args.no_store:
            stores = await stack.enter_async_context(open_stores(settings))
            if stores is None:
                log.error("DATABASE_URL must be set (or pass --no-store)")
                return USAGE
        sender = await stack.enter_async_context(open_sender(settings)) if send else None
        if stores is None:
            today = args.date or local_today(settings)
            level = cast(Cefr, args.level or settings.learner_cefr)
            return await preview_topic(
                settings, today, topic_id=args.topic, level=level, sender=sender, chat_id=chat_id
            )
        assert sender is not None
        learner = await stores.learners.get_or_create(chat_id, settings.learner_cefr, settings.default_schedule())
        today = args.date or local_now(learner.timezone, datetime.now(UTC)).date()
        cefr = resolve_cefr(args.level, learner, settings.learner_cefr)
        with bind(chat_id=chat_id):
            return await run_lesson_for_learner(
                Runtime(settings, stores, sender), learner, today, topic_id=args.topic, cefr=cefr
            )
    return OK  # unreachable; keeps mypy's return analysis simple


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    # Tracing is a no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set; shutdown() flushes
    # the batched spans, which matters because this process exits right after main().
    provider = configure_tracing(settings)
    try:
        return asyncio.run(_run(settings, args))
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
