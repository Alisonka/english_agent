"""The server process: the /level bot and the daily lesson in one long-running program.

    uv run python -m english_agent.app

The main thread long-polls Telegram for /level, /mylevel, quiz answers and translation
answers (this process must be the bot's only `getUpdates` consumer). A background
APScheduler thread runs the daily pipeline at LESSON_SEND_TIME in TIMEZONE; started after
that time, it runs the pipeline right away, and `run_daily` skips a day whose lesson is
already sent. Every few minutes it also sends the practice that is due (see `practice`).
SIGTERM (`docker stop`) stops both and waits for a lesson in progress.
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from english_agent.config import Settings, get_settings
from english_agent.pipeline import local_today, run_daily
from english_agent.practice import run_practice_tick
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import Stores, sql_stores
from english_agent.telegram.leveling import serve_leveling
from english_agent.tracing import configure_tracing

DAILY_JOB_ID = "daily_lesson"
PRACTICE_JOB_ID = "practice"
_MISFIRE_GRACE_SECONDS = 6 * 3600
_PRACTICE_TICK_MINUTES = 5


def run_scheduled_lesson(settings: Settings, stores: Stores) -> None:
    today = local_today(settings)
    code = run_daily(settings, stores, today)
    print(f"daily lesson for {today.isoformat()}: exit code {code}", file=sys.stderr)


def schedule_daily_lesson(scheduler: BaseScheduler, settings: Settings, stores: Stores, now: datetime) -> Job:
    """Add the daily job; if `now` is already past today's send time, its first run is `now` (catch-up)."""
    send_time = settings.lesson_send_time
    trigger = CronTrigger(hour=send_time.hour, minute=send_time.minute, timezone=ZoneInfo(settings.timezone))
    catch_up: dict[str, Any] = {}
    if now.time() >= send_time:
        catch_up["next_run_time"] = now
    return scheduler.add_job(
        run_scheduled_lesson,
        trigger,
        args=(settings, stores),
        id=DAILY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        **catch_up,
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.telegram_configured():
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2
    if not settings.database_url.get_secret_value():
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    # Docker stops the container with SIGTERM; turn it into the same path as Ctrl+C.
    signal.signal(signal.SIGTERM, _interrupt)
    provider = configure_tracing(settings)
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))  # fail fast on a wrong DATABASE_URL
        stores = sql_stores(make_session_factory(engine))

        timezone = ZoneInfo(settings.timezone)
        scheduler = BackgroundScheduler(timezone=timezone)
        job = schedule_daily_lesson(scheduler, settings, stores, datetime.now(timezone))
        schedule_practice(scheduler, settings, stores)
        scheduler.start()
        print(f"daily lesson scheduled, next run {job.next_run_time}", file=sys.stderr)
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
