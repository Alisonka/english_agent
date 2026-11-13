"""Storage maintenance commands.

uv run python -m english_agent.storage import-learner-json                    # .state/learner.json -> learners
uv run python -m english_agent.storage import-learner-json - < learner.json   # read the file from stdin
uv run python -m english_agent.storage backfill-items                         # core items for already sent lessons
uv run python -m english_agent.storage backfill-reviews                       # FSRS ratings for already stored answers
uv run python -m english_agent.storage set-schedule --chat 42 --lesson-time 08:30  # one learner's schedule
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, time
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import get_settings
from english_agent.items import backfill_core_items
from english_agent.logs import configure_logging
from english_agent.models.cefr import Cefr
from english_agent.models.schedule import LearnerSchedule, parse_clock
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import MaintenanceRepo, open_stores

log = logging.getLogger(__name__)


class _LegacyLearnerState(BaseModel):
    """Shape of the `.state/learner.json` file the bot wrote before the database existed."""

    cefr: Cefr


async def _import_learner_json(path: str) -> int:
    settings = get_settings()
    if not settings.telegram_chat_id:
        log.error("TELEGRAM_CHAT_ID must be set: the learner is stored under that chat")
        return 2
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        state = _LegacyLearnerState.model_validate_json(text)
    except (OSError, ValidationError) as error:
        log.error("cannot read learner state from %s: %s", path, error)
        return 1
    async with open_stores(settings) as stores:
        if stores is None:
            log.error("DATABASE_URL must be set")
            return 2
        record = await stores.learners.set_cefr(settings.telegram_chat_id, state.cefr)
    print(f"learner {record.id} (chat {record.telegram_chat_id}): cefr={record.cefr}")
    return 0


async def _backfill_items() -> int:
    async with open_stores(get_settings()) as stores:
        if stores is None:
            log.error("DATABASE_URL must be set")
            return 2
        try:
            result = await backfill_core_items(stores)
        except SQLAlchemyError as error:
            log.error("backfill failed: %s", error)
            return 1
    print(f"items: {result.created} created, {result.existing} already present, {result.skipped} lessons skipped")
    return 0


async def _backfill_reviews() -> int:
    """Run after `backfill-items`: links old polls to items, then rates every stored answer once."""
    settings = get_settings()
    if not settings.database_url.get_secret_value():
        log.error("DATABASE_URL must be set")
        return 2
    engine = make_engine(settings)
    maintenance = MaintenanceRepo(make_session_factory(engine), make_scheduler(settings))
    try:
        linked = await maintenance.link_polls_to_items()
        applied = await maintenance.replay_reviews()
    except SQLAlchemyError as error:
        log.error("backfill failed: %s", error)
        return 1
    finally:
        await engine.dispose()
    print(f"reviews: {linked} polls linked to items, {applied} ratings applied")
    return 0


def _clock(text: str) -> time:
    clock = parse_clock(text)
    if clock is None:
        raise argparse.ArgumentTypeError(f"not a time of day: {text!r} (use HH:MM)")
    return clock


async def _set_schedule(args: argparse.Namespace) -> int:
    """Set one learner's schedule; every option left out takes its value from the settings (.env)."""
    settings = get_settings()
    chat_id = args.chat or settings.telegram_chat_id
    if not chat_id:
        log.error("pass --chat or set TELEGRAM_CHAT_ID")
        return 2
    defaults = settings.default_schedule()
    try:
        schedule = LearnerSchedule(
            timezone=args.timezone or defaults.timezone,
            lesson_send_time=args.lesson_time or defaults.lesson_send_time,
            translation_send_time=args.translation_time or defaults.translation_send_time,
            quiz_delay_minutes=defaults.quiz_delay_minutes if args.quiz_delay is None else args.quiz_delay,
        )
    except ValidationError as error:
        log.error("invalid schedule: %s", error)
        return 2
    async with open_stores(settings) as stores:
        if stores is None:
            log.error("DATABASE_URL must be set")
            return 2
        try:
            if await stores.learners.get(chat_id) is None:
                log.error("no learner with chat id %s", chat_id)
                return 1
            record = await stores.learners.set_schedule(chat_id, schedule)
            if args.started_on is not None:
                record = await stores.learners.start_course(chat_id, args.started_on)
        except SQLAlchemyError as error:
            log.error("could not set the schedule: %s", error)
            return 1
    assert record is not None
    print(
        f"learner {record.id} (chat {record.telegram_chat_id}): {record.timezone}, "
        f"lesson {record.lesson_send_time:%H:%M}, translation {record.translation_send_time:%H:%M}, "
        f"quiz +{record.quiz_delay_minutes} min, started {record.curriculum_started_on}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storage maintenance commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    learner_json = commands.add_parser(
        "import-learner-json", help="copy the level from the old .state/learner.json into the learners table"
    )
    learner_json.add_argument("path", nargs="?", default=".state/learner.json", help="state file, or - for stdin")
    commands.add_parser("backfill-items", help="create the core item of every sent lesson that has none")
    commands.add_parser("backfill-reviews", help="rate stored quiz and translation answers that have no review yet")
    schedule = commands.add_parser(
        "set-schedule", help="set a learner's schedule; options left out take the values from the settings"
    )
    schedule.add_argument("--chat", help="the learner's chat id (default: TELEGRAM_CHAT_ID)")
    schedule.add_argument("--timezone", help="IANA time zone, e.g. Europe/Moscow (default: TIMEZONE)")
    schedule.add_argument("--lesson-time", type=_clock, help="HH:MM (default: LESSON_SEND_TIME)")
    schedule.add_argument("--translation-time", type=_clock, help="HH:MM (default: TRANSLATION_SEND_TIME)")
    schedule.add_argument("--quiz-delay", type=int, help="minutes after the lesson (default: QUIZ_DELAY_MINUTES)")
    schedule.add_argument(
        "--started-on", type=date.fromisoformat, help="course start YYYY-MM-DD, only if the learner has none"
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings())

    if args.command == "import-learner-json":
        return asyncio.run(_import_learner_json(args.path))
    if args.command == "backfill-items":
        return asyncio.run(_backfill_items())
    if args.command == "backfill-reviews":
        return asyncio.run(_backfill_reviews())
    if args.command == "set-schedule":
        return asyncio.run(_set_schedule(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
