"""When is what due: the planner's rules across time zones, DST and failure states."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from english_agent.config import Settings
from english_agent.models.storage import LearnerRecord, LessonState, LessonStatus, PracticeKind
from english_agent.planning import lesson_due, practice_due, retry_backoff

START = date(2026, 11, 1)
MSK = ZoneInfo("Europe/Moscow")


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def learner(**changes: Any) -> LearnerRecord:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    values: dict[str, Any] = {
        "id": 1,
        "telegram_chat_id": "42",
        "cefr": "A2",
        "created_at": now,
        "updated_at": now,
        "curriculum_started_on": START,
    }
    return LearnerRecord.model_validate(values | changes)


def at(hour: int, minute: int = 0, day: date = START, zone: ZoneInfo = MSK) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=zone)


def state(
    status: LessonStatus, *, attempts: int = 1, claimed_at: datetime | None = None, sent_at: datetime | None = None
) -> LessonState:
    return LessonState(lesson_id=1, status=status, attempts=attempts, claimed_at=claimed_at, sent_at=sent_at)


# --- the lesson ---------------------------------------------------------------------------


def test_lesson_is_due_from_send_time_until_the_catch_up_window_closes() -> None:
    rules = settings(lesson_catch_up_hours=6)
    assert not lesson_due(learner(), None, at(8, 59), rules)
    assert lesson_due(learner(), None, at(9, 0), rules)
    assert lesson_due(learner(), None, at(14, 59), rules)
    assert not lesson_due(learner(), None, at(15, 0), rules)


@pytest.mark.parametrize("status", ["sent", "skipped"])
def test_a_finished_day_is_never_due_again(status: LessonStatus) -> None:
    assert not lesson_due(learner(), state(status, claimed_at=at(9)), at(10), settings())


def test_a_failed_day_waits_a_growing_backoff_and_stops_after_the_last_attempt() -> None:
    failed = state("failed", attempts=2, claimed_at=at(9, 0))
    assert retry_backoff(2) == timedelta(minutes=4)
    assert not lesson_due(learner(), failed, at(9, 3), settings())
    assert lesson_due(learner(), failed, at(9, 4), settings())
    exhausted = state("failed", attempts=5, claimed_at=at(9, 0))
    assert not lesson_due(learner(), exhausted, at(12, 0), settings(job_max_attempts=5))


def test_a_claim_is_respected_until_its_worker_looks_dead() -> None:
    sending = state("sending", claimed_at=at(9, 0))
    assert not lesson_due(learner(), sending, at(9, 9), settings(lesson_claim_stale_minutes=10))
    assert lesson_due(learner(), sending, at(9, 10), settings(lesson_claim_stale_minutes=10))


def test_a_released_claim_is_due_on_the_next_tick() -> None:
    assert lesson_due(learner(), state("pending", claimed_at=None), at(9, 1), settings())


def test_lesson_follows_the_learners_own_time_zone_and_send_time() -> None:
    vladivostok = learner(timezone="Asia/Vladivostok", lesson_send_time=time(7, 30))
    # 07:30 in Vladivostok is 00:30 in Moscow.
    assert lesson_due(vladivostok, None, at(0, 30, START + timedelta(days=1)), settings())
    assert not lesson_due(vladivostok, None, at(9, 0), settings())


def test_lesson_time_is_wall_clock_time_across_a_dst_change() -> None:
    new_york = ZoneInfo("America/New_York")
    commuter = learner(timezone="America/New_York", lesson_send_time=time(9, 0))
    # 2026-11-01 is the day New York leaves daylight saving time: 09:00 there is 14:00 UTC, not 13:00.
    assert lesson_due(commuter, None, at(9, 0, date(2026, 11, 1), new_york), settings())
    assert not lesson_due(commuter, None, datetime(2026, 11, 1, 13, 30, tzinfo=UTC), settings())
    assert lesson_due(commuter, None, datetime(2026, 11, 1, 14, 0, tzinfo=UTC), settings())


@pytest.mark.parametrize(
    "changes",
    [
        {"curriculum_started_on": None},
        {"curriculum_started_on": START + timedelta(days=1)},
        {"paused_at": datetime(2026, 10, 30, tzinfo=UTC)},
        {"is_active": False, "deactivated_reason": "blocked"},
    ],
)
def test_no_lesson_before_the_start_nor_while_paused_or_inactive(changes: dict[str, Any]) -> None:
    assert not lesson_due(learner(**changes), None, at(10), settings())


# --- practice ----------------------------------------------------------------------------


def sent(at_: datetime) -> LessonState:
    return state("sent", sent_at=at_)


def test_quiz_may_be_due_once_its_delay_after_the_lesson_has_passed() -> None:
    lesson = sent(at(9, 0))
    assert not practice_due(learner(), lesson, set(), at(9, 59), settings())
    assert practice_due(learner(), lesson, set(), at(10, 0), settings())


def test_translation_may_be_due_from_its_time_once_the_quiz_was_sent() -> None:
    lesson = sent(at(9, 0))
    quiz_sent: set[PracticeKind] = {"quiz"}
    assert not practice_due(learner(), lesson, quiz_sent, at(17, 59), settings())
    assert practice_due(learner(), lesson, quiz_sent, at(18, 0), settings())
    both: set[PracticeKind] = {"quiz", "translation"}
    assert not practice_due(learner(), lesson, both, at(19, 0), settings())


def test_no_practice_after_the_cutoff_nor_without_a_sent_lesson() -> None:
    assert not practice_due(learner(), sent(at(21, 0)), set(), at(22, 0), settings())
    assert not practice_due(learner(), state("sending", claimed_at=at(9)), set(), at(12), settings())
    assert not practice_due(learner(), None, set(), at(12), settings())
