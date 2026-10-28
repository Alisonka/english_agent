"""Topic -> research -> lesson -> Telegram. The one place that wires the steps together.

uv run python -m english_agent.pipeline                                 # topic of the day, send
uv run python -m english_agent.pipeline --date 2026-11-05 --no-send     # topic of that day, print only
uv run python -m english_agent.pipeline --topic there_is_are            # a specific topic, send
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import date

import httpx
from opentelemetry.trace import Span, Status, StatusCode

from english_agent.config import Settings, get_settings
from english_agent.llm import AnthropicLLM
from english_agent.models.curriculum import Curriculum, CurriculumTopic, load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.telegram import TelegramClient, TelegramError
from english_agent.tracing import configure_tracing, get_tracer
from english_agent.writer import write_lesson

COURSE_FINISHED = "Курс завершён: все темы пройдены."


def _http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


def generate_lesson(
    topic: CurriculumTopic, settings: Settings, *, curriculum_version: int | None = None
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
        )
        research = researcher.run(topic)

    if research.notes is None:
        return research, None
    lesson = write_lesson(
        AnthropicLLM(settings, model=settings.writer_model),
        topic,
        research.notes,
        curriculum_version=curriculum_version,
    )
    return research, lesson


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
        print(f"telegram error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research a topic, write the lesson and send it to Telegram.")
    parser.add_argument("--topic", help="topic id from curriculum/a2_topics.yaml (default: topic of the day)")
    parser.add_argument(
        "--date", type=date.fromisoformat, help="pick the topic of this day, YYYY-MM-DD (default: today)"
    )
    parser.add_argument("--no-send", action="store_true", help="print the lesson but do not send it to Telegram")
    args = parser.parse_args(argv)

    curriculum = load_curriculum()
    settings = get_settings()

    # Tracing is a no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set; shutdown() flushes
    # the batched spans, which matters because this process exits right after main().
    provider = configure_tracing(settings)
    try:
        with get_tracer().start_as_current_span("pipeline.run") as span:
            span.set_attribute("pipeline.send", not args.no_send)
            return _run(args, curriculum, settings, span)
    finally:
        if provider is not None:
            provider.shutdown()


def _run(args: argparse.Namespace, curriculum: Curriculum, settings: Settings, span: Span) -> int:
    send = not args.no_send

    if args.topic:
        topic = next((item for item in curriculum.topics if item.id == args.topic), None)
        if topic is None:
            print(f"unknown topic {args.topic!r}", file=sys.stderr)
            return 2
    else:
        start = settings.curriculum_start_date
        if start is None:
            print("CURRICULUM_START_DATE must be set (or pass --topic)", file=sys.stderr)
            return 2
        day = ((args.date or date.today()) - start).days
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
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or pass --no-send)", file=sys.stderr)
        return 2

    lesson: Lesson | None = None
    try:
        research, lesson = generate_lesson(topic, settings, curriculum_version=curriculum.version)
        reason = research.message
    except Exception as error:  # unattended run: every failure has to reach the chat, not just the CI log
        reason = f"{type(error).__name__}: {error}"
    if lesson is None:
        print(f"lesson for {topic.id} failed: {reason}", file=sys.stderr)
        span.set_status(Status(StatusCode.ERROR, reason))
        if send:
            notify(settings, f"Урок «{topic.title_ru}» сегодня не получился: {reason}")
        return 1
    print(lesson.model_dump_json(indent=2, ensure_ascii=False))
    if args.no_send:
        return 0

    try:
        message_ids = deliver_lesson(lesson, settings)
    except TelegramError as error:
        print(f"telegram error: {error}", file=sys.stderr)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        return 1
    print(f"sent {len(message_ids)} message(s) to Telegram chat {settings.telegram_chat_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
