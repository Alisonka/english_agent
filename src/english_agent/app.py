"""The server process: the bot and the scheduled lessons and practice on one event loop.

    uv run python -m english_agent.app

The bot long-polls Telegram for commands, quiz answers and translation answers (this process
must be the bot's only `getUpdates` consumer); updates are handled concurrently. APScheduler's
asyncio scheduler checks every minute whose lesson is due on their own schedule (see
`pipeline.run_lesson_tick`) and every few minutes which practice is due (see `practice`). State
lives in the database, so a restart neither loses nor repeats a lesson.

SIGTERM (`docker stop`) stops polling, lets running ticks and gradings finish for up to
SHUTDOWN_GRACE_SECONDS, then closes the bot, the Claude client and the database pool.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from anthropic import AsyncAnthropic
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from english_agent.bot.app import ALLOWED_UPDATES, build_dispatcher
from english_agent.bot.deps import BotDeps, InProcessJobs
from english_agent.config import Settings, get_settings
from english_agent.grading import make_grader
from english_agent.llm import make_client
from english_agent.logs import configure_logging
from english_agent.paths import available_cefr_levels
from english_agent.pipeline import Runtime, run_lesson_tick
from english_agent.practice import deliver_due_practice
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import sql_stores
from english_agent.telegram.sender import Sender, make_bot
from english_agent.tracing import configure_tracing

LESSON_JOB_ID = "lessons"
PRACTICE_JOB_ID = "practice"
_LESSON_TICK_MINUTES = 1
_PRACTICE_TICK_MINUTES = 5
SHUTDOWN_GRACE_SECONDS = 90  # below compose's stop_grace_period
MAX_CONCURRENT_UPDATES = 100  # updates handled at once; more wait in aiogram's queue

log = logging.getLogger(__name__)


class Ticks:
    """The scheduled ticks running right now, so shutdown can wait for them (APScheduler 3 does not)."""

    def __init__(self) -> None:
        self.running: set[asyncio.Task[Any]] = set()

    def tracked(self, tick: Callable[[Runtime], Awaitable[None]]) -> Callable[[Runtime], Awaitable[None]]:
        async def run(runtime: Runtime) -> None:
            task = asyncio.current_task()
            assert task is not None
            self.running.add(task)
            try:
                await tick(runtime)
            except Exception:  # a failed tick is logged; the next one runs as usual
                log.exception("%s failed", tick.__name__)
            finally:
                self.running.discard(task)

        run.__name__ = tick.__name__
        return run

    async def wait(self, timeout: float) -> None:
        if not self.running:
            return
        _, still = await asyncio.wait(set(self.running), timeout=timeout)
        for task in still:
            task.cancel()  # claims they held are taken over once stale
        if still:
            log.warning("cancelled %d tick(s) still running on shutdown", len(still))
            await asyncio.gather(*still, return_exceptions=True)


async def run_scheduled_lessons(runtime: Runtime) -> None:
    ran = await run_lesson_tick(runtime, datetime.now(UTC))
    if ran:
        log.info("lesson tick: %d lesson run(s)", ran)


async def run_scheduled_practice(runtime: Runtime) -> None:
    sent = await deliver_due_practice(runtime.sender, runtime.settings, runtime.stores, datetime.now(UTC))
    if sent:
        log.info("practice tick: %d block(s) sent", len(sent))


def schedule_lessons(scheduler: BaseScheduler, runtime: Runtime, ticks: Ticks) -> Job:
    """Check every minute whose lesson is due; a tick still running makes the next one wait."""
    return scheduler.add_job(
        ticks.tracked(run_scheduled_lessons),
        IntervalTrigger(minutes=_LESSON_TICK_MINUTES),
        args=(runtime,),
        id=LESSON_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),  # right away: a restart must not wait for the first interval
    )


def schedule_practice(scheduler: BaseScheduler, runtime: Runtime, ticks: Ticks) -> Job:
    """Check every few minutes whether the quiz or the translation task is due; state lives in the database."""
    return scheduler.add_job(
        ticks.tracked(run_scheduled_practice),
        IntervalTrigger(minutes=_PRACTICE_TICK_MINUTES),
        args=(runtime,),
        id=PRACTICE_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


async def serve(settings: Settings) -> int:
    engine = make_engine(settings)
    client: AsyncAnthropic = make_client(settings)
    bot = make_bot(settings)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))  # fail fast on a wrong DATABASE_URL
        stores = sql_stores(make_session_factory(engine), make_scheduler(settings))
        sender = Sender(bot)
        runtime = Runtime(settings, stores, sender, client=client)
        jobs = InProcessJobs(stores, sender, make_grader(settings, client))
        dispatcher = build_dispatcher(BotDeps(settings, stores, sender, jobs, available_cefr_levels()))

        scheduler, ticks = AsyncIOScheduler(timezone=UTC), Ticks()
        schedule_lessons(scheduler, runtime, ticks)
        schedule_practice(scheduler, runtime, ticks)
        scheduler.start()
        log.info("lessons and practice scheduled; polling for updates")
        try:
            # Returns on SIGINT/SIGTERM once the updates being handled are done.
            await dispatcher.start_polling(
                bot,
                allowed_updates=ALLOWED_UPDATES,
                handle_signals=True,
                close_bot_session=False,
                tasks_concurrency_limit=MAX_CONCURRENT_UPDATES,
            )
        finally:
            scheduler.shutdown(wait=False)  # no new ticks
            await ticks.wait(SHUTDOWN_GRACE_SECONDS)
            await jobs.drain(SHUTDOWN_GRACE_SECONDS)
        return 0
    finally:
        await bot.session.close()
        await client.close()
        await engine.dispose()


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    if not settings.telegram_bot_token.get_secret_value():
        log.error("TELEGRAM_BOT_TOKEN must be set")
        return 2
    if not settings.database_url.get_secret_value():
        log.error("DATABASE_URL must be set")
        return 2
    provider = configure_tracing(settings)
    try:
        return asyncio.run(serve(settings))
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
