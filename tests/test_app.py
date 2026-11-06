from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from english_agent import app
from english_agent.config import Settings
from english_agent.storage import Stores
from fakes import fake_stores

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture
def scheduler() -> Iterator[BackgroundScheduler]:
    """A started but paused scheduler: jobs get their next run time, nothing actually runs."""
    scheduler = BackgroundScheduler(timezone=MOSCOW)
    scheduler.start(paused=True)
    yield scheduler
    scheduler.shutdown(wait=False)


def settings() -> Settings:
    return Settings(_env_file=None, lesson_send_time=time(9, 0), timezone="Europe/Moscow")


def test_started_after_send_time_catches_up_now(scheduler: BackgroundScheduler) -> None:
    now = datetime(2026, 11, 3, 12, 30, tzinfo=MOSCOW)
    job = app.schedule_daily_lesson(scheduler, settings(), fake_stores()[0], now)
    assert job.next_run_time == now


def test_started_before_send_time_waits_for_it(scheduler: BackgroundScheduler) -> None:
    now = datetime(2026, 11, 3, 8, 59, tzinfo=MOSCOW)
    job = app.schedule_daily_lesson(scheduler, settings(), fake_stores()[0], now)
    assert job.next_run_time != now
    assert job.next_run_time.astimezone(MOSCOW).time() == time(9, 0)


def test_daily_job_runs_one_at_a_time(scheduler: BackgroundScheduler) -> None:
    now = datetime(2026, 11, 3, 12, 30, tzinfo=MOSCOW)
    app.schedule_daily_lesson(scheduler, settings(), fake_stores()[0], now)
    app.schedule_daily_lesson(scheduler, settings(), fake_stores()[0], now)  # replaces, does not add a second job

    [job] = scheduler.get_jobs()
    assert (job.id, job.max_instances, job.coalesce) == (app.DAILY_JOB_ID, 1, True)


def test_scheduled_run_uses_today_in_the_configured_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    days: list[date] = []

    def fake_run_daily(settings: Settings, stores: Stores, today: date) -> int:
        days.append(today)
        return 0

    monkeypatch.setattr(app, "run_daily", fake_run_daily)
    monkeypatch.setattr(app, "local_today", lambda settings: date(2026, 11, 3))

    app.run_scheduled_lesson(settings(), fake_stores()[0])

    assert days == [date(2026, 11, 3)]


def test_practice_tick_runs_every_few_minutes_one_at_a_time(scheduler: BackgroundScheduler) -> None:
    app.schedule_practice(scheduler, settings(), fake_stores()[0])
    app.schedule_practice(scheduler, settings(), fake_stores()[0])  # replaces, does not add a second job

    [job] = scheduler.get_jobs()
    assert job.id == app.PRACTICE_JOB_ID
    assert job.max_instances == 1
    assert job.trigger.interval.total_seconds() == 5 * 60
