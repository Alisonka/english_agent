from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from english_agent.config import Settings
from english_agent.srs import AGAIN, GOOD, ReviewScheduler, due_at, make_scheduler, quiz_rating, translation_rating

AT = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def scheduler() -> ReviewScheduler:
    return ReviewScheduler(enable_fuzzing=False)


def test_ratings_map_answers_to_fsrs() -> None:
    assert (quiz_rating(True), quiz_rating(False)) == (GOOD, AGAIN)
    assert translation_rating("correct") == GOOD
    assert translation_rating("fixed") == AGAIN
    assert translation_rating("missing") is None


def test_new_card_is_due_at_creation() -> None:
    state = scheduler().new_card(AT)
    assert due_at(state) == AT


def test_a_good_answer_schedules_days_ahead_and_the_gap_grows() -> None:
    s = scheduler()
    first, _ = s.review(s.new_card(AT), GOOD, AT)
    second, _ = s.review(first, GOOD, due_at(first))

    first_gap = due_at(first) - AT
    second_gap = due_at(second) - due_at(first)
    assert timedelta(days=1) <= first_gap < second_gap  # no 1-minute learning steps for a daily lesson


def test_a_wrong_answer_brings_the_review_back_soon() -> None:
    s = scheduler()
    learned, _ = s.review(s.new_card(AT), GOOD, AT)
    learned, _ = s.review(learned, GOOD, due_at(learned))
    forgotten, log = s.review(learned, AGAIN, due_at(learned))

    assert due_at(forgotten) - due_at(learned) <= timedelta(days=2)
    assert log["rating"] == AGAIN
    assert s.retrievability(forgotten, due_at(forgotten)) < 1


def test_review_times_are_normalised_to_utc() -> None:
    s = scheduler()
    moscow = AT.astimezone(ZoneInfo("Europe/Moscow"))
    state = s.new_card(moscow)
    assert due_at(state) == AT
    reviewed, log = s.review(state, GOOD, moscow)
    assert log["review_datetime"].endswith("+00:00")
    assert due_at(reviewed).tzinfo is not None


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler().new_card(datetime(2026, 11, 11, 9, 0))


def test_scheduler_from_settings_caps_the_interval() -> None:
    s = make_scheduler(Settings(_env_file=None, fsrs_maximum_interval_days=3))
    state = s.new_card(AT)
    for _ in range(6):
        state, _ = s.review(state, GOOD, due_at(state))
    assert due_at(state) - datetime.fromisoformat(state["last_review"]) <= timedelta(days=3)
