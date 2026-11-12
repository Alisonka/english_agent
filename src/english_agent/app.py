"""The server process: the bot and the scheduled lessons and practice in one long-running program.

    uv run python -m english_agent.app

The main thread long-polls Telegram for commands, quiz answers and translation answers (this
process must be the bot's only `getUpdates` consumer). A background APScheduler thread checks
every minute whose lesson is due on their own schedule (see `pipeline.run_lesson_tick`), and
every few minutes which practice is due (see `practice`). State lives in the database, so a
restart neither loses nor repeats a lesson. SIGTERM (`docker stop`) stops both and waits for a
lesson in progress.
"""

from __future__ import annotations

import logging
import signal
from datetime import UTC, datetime
from types import FrameType

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from english_agent.config import Settings, get_settings
from english_agent.logs import configure_logging
from english_agent.pipeline import run_lesson_tick
from english_agent.practice import run_practice_tick
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import Stores, sql_stores
from english_agent.telegram.leveling import serve_leveling
from english_agent.tracing import configure_tracing

LESSON_JOB_ID = "lessons"
PRACTICE_JOB_ID = "practice"
_LESSON_TICK_MINUTES = 1
_PRACTICE_TICK_MINUTES = 5

log = logging.getLogger(__name__)


def run_scheduled_lessons(settings: Settings, stores: Stores) -> None:
    ran = run_lesson_tick(settings, stores, datetime.now(UTC))
    if ran:
        log.info("lesson tick: %d lesson run(s)", ran)


def schedule_lessons(scheduler: BaseScheduler, settings: Settings, stores: Stores) -> Job:
    """Check every minute whose lesson is due; a tick still running makes the next one wait."""
    return scheduler.add_job(
        run_scheduled_lessons,
        IntervalTrigger(minutes=_LESSON_TICK_MINUTES),
        args=(settings, stores),
        id=LESSON_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),  # right away: a restart must not wait for the first interval
    )


def schedule_practice(scheduler: BaseScheduler, settings: Settings, stores: Stores) -> Job:
    """Check every few minutes whether the quiz or the translation task is due; state lives in the database."""
    return scheduler.add_job(
        run_practice_tick,
        IntervalTrigger(minutes=_PRACTICE_TICK_MINUTES),
        args=(settings, stores),
        id=PRACTICE_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    if not settings.telegram_bot_token.get_secret_value():
        log.error("TELEGRAM_BOT_TOKEN must be set")
        return 2
    if not settings.database_url.get_secret_value():
        log.error("DATABASE_URL must be set")
        return 2

    # Docker stops the container with SIGTERM; turn it into the same path as Ctrl+C.
    signal.signal(signal.SIGTERM, _interrupt)
    provider = configure_tracing(settings)
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))  # fail fast on a wrong DATABASE_URL
        stores = sql_stores(make_session_factory(engine), make_scheduler(settings))

        scheduler = BackgroundScheduler(timezone=UTC)
        schedule_lessons(scheduler, settings, stores)
        schedule_practice(scheduler, settings, stores)
        scheduler.start()
        log.info("lessons and practice scheduled")
        try:
            return serve_leveling(settings, stores)
        finally:
            scheduler.shutdown(wait=True)
    finally:
        engine.dispose()
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
