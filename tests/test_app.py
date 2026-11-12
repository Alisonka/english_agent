from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from english_agent import app
from english_agent.config import Settings
from english_agent.storage import Stores
from fakes import fake_stores


@pytest.fixture
def scheduler() -> Iterator[BackgroundScheduler]:
    """A started but paused scheduler: jobs get their next run time, nothing actually runs."""
    scheduler = BackgroundScheduler(timezone=UTC)
    scheduler.start(paused=True)
    yield scheduler
    scheduler.shutdown(wait=False)


def settings() -> Settings:
    return Settings(_env_file=None)


def test_lesson_tick_runs_every_minute_one_at_a_time_starting_now(scheduler: BackgroundScheduler) -> None:
    before = datetime.now(UTC)
    app.schedule_lessons(scheduler, settings(), fake_stores()[0])
    app.schedule_lessons(scheduler, settings(), fake_stores()[0])  # replaces, does not add a second job

    [job] = scheduler.get_jobs()
    assert (job.id, job.max_instances, job.coalesce) == (app.LESSON_JOB_ID, 1, True)
    assert job.trigger.interval.total_seconds() == 60
    assert job.next_run_time >= before


def test_scheduled_lessons_check_every_learner_now(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[datetime] = []

    def fake_tick(settings: Settings, stores: Stores, now: datetime) -> int:
        calls.append(now)
        return 0

    monkeypatch.setattr(app, "run_lesson_tick", fake_tick)
    app.run_scheduled_lessons(settings(), fake_stores()[0])

    [now] = calls
    assert now.tzinfo is not None


def test_practice_tick_runs_every_few_minutes_one_at_a_time(scheduler: BackgroundScheduler) -> None:
    app.schedule_practice(scheduler, settings(), fake_stores()[0])
    app.schedule_practice(scheduler, settings(), fake_stores()[0])  # replaces, does not add a second job

    [job] = scheduler.get_jobs()
    assert job.id == app.PRACTICE_JOB_ID
    assert job.max_instances == 1
    assert job.trigger.interval.total_seconds() == 5 * 60
