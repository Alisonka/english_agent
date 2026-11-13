from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from english_agent import app
from english_agent.config import Settings
from english_agent.pipeline import Runtime
from fakes import fake_stores
from telegram_fake import FakeTelegram, fake_sender


@pytest.fixture
async def scheduler() -> AsyncIterator[AsyncIOScheduler]:
    """A started but paused scheduler: jobs get their next run time, nothing actually runs."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.start(paused=True)
    yield scheduler
    scheduler.shutdown(wait=False)


def runtime() -> Runtime:
    return Runtime(Settings(_env_file=None), fake_stores()[0], fake_sender(FakeTelegram()))


async def test_lesson_tick_runs_every_minute_one_at_a_time_starting_now(scheduler: AsyncIOScheduler) -> None:
    before = datetime.now(UTC)
    ticks = app.Ticks()
    app.schedule_lessons(scheduler, runtime(), ticks)
    app.schedule_lessons(scheduler, runtime(), ticks)  # replaces, does not add a second job

    [job] = scheduler.get_jobs()
    assert (job.id, job.max_instances, job.coalesce) == (app.LESSON_JOB_ID, 1, True)
    assert job.trigger.interval.total_seconds() == 60
    assert job.next_run_time >= before


async def test_practice_tick_runs_every_few_minutes_one_at_a_time(scheduler: AsyncIOScheduler) -> None:
    app.schedule_practice(scheduler, runtime(), app.Ticks())
    app.schedule_practice(scheduler, runtime(), app.Ticks())

    [job] = scheduler.get_jobs()
    assert (job.id, job.max_instances) == (app.PRACTICE_JOB_ID, 1)
    assert job.trigger.interval.total_seconds() == 5 * 60


async def test_a_failing_tick_is_logged_and_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(runtime: Runtime) -> None:
        raise RuntimeError("database is down")

    await app.Ticks().tracked(broken)(runtime())
    assert "database is down" in caplog.text


async def test_shutdown_waits_for_a_running_tick_then_cancels_an_overrunning_one() -> None:
    ticks, finished = app.Ticks(), []

    async def short(runtime: Runtime) -> None:
        await asyncio.sleep(0.01)
        finished.append("short")

    async def endless(runtime: Runtime) -> None:
        await asyncio.sleep(60)
        finished.append("endless")

    running = [asyncio.create_task(ticks.tracked(tick)(runtime())) for tick in (short, endless)]
    await asyncio.sleep(0)
    await ticks.wait(timeout=0.2)

    assert finished == ["short"]
    assert all(task.done() for task in running) and not ticks.running
