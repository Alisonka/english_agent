from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from english_agent.models.lesson import Lesson
from english_agent.models.storage import LessonRecord
from english_agent.storage import (
    CardRepo,
    ItemRepo,
    LearnerRepo,
    LessonAlreadySentError,
    LessonRepo,
    SessionFactory,
    session_scope,
)
from english_agent.storage.orm import LessonRow
from english_agent.versions import SCHEMA_VERSIONS

DAY = date(2026, 11, 3)


def test_learner_get_or_create_is_idempotent(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    assert learners.get("42") is None

    created = learners.get_or_create("42", "A2")
    again = learners.get_or_create("42", "B1")  # the default only applies to a new learner

    assert again.id == created.id
    assert again.cefr == "A2"
    assert learners.get("42") == created


def test_learner_set_cefr_updates_or_creates(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    created = learners.set_cefr("42", "B1")
    assert created.cefr == "B1"

    updated = learners.set_cefr("42", "C1")
    assert (updated.id, updated.cefr) == (created.id, "C1")
    assert learners.get("42") == updated


def test_lesson_round_trips_through_jsonb(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)

    saved = lessons.record_sent(learner.id, DAY, lesson, [10, 11], curriculum_version=3)
    loaded = lessons.get(learner.id, DAY)

    assert isinstance(loaded, LessonRecord)
    assert loaded == saved
    assert loaded.lesson == lesson
    assert loaded.status == "sent"
    assert loaded.telegram_message_ids == [10, 11]
    assert loaded.schema_version == SCHEMA_VERSIONS["lesson"]
    assert loaded.curriculum_version == 3
    assert loaded.sent_at is not None


def test_failed_lesson_can_be_retried_but_a_sent_one_is_final(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)

    failed = lessons.record_failed(
        learner.id, DAY, topic_id=lesson.topic_id, cefr="A2", reason="boom", curriculum_version=3
    )
    assert (failed.status, failed.lesson, failed.error) == ("failed", None, "boom")
    assert not lessons.sent_on(learner.id, DAY)

    sent = lessons.record_sent(learner.id, DAY, lesson, [1], curriculum_version=3)
    assert sent.id == failed.id
    assert sent.error is None
    assert lessons.sent_on(learner.id, DAY)
    assert not lessons.sent_on(learner.id, DAY + timedelta(days=1))

    with pytest.raises(LessonAlreadySentError):
        lessons.record_sent(learner.id, DAY, lesson, [2], curriculum_version=3)


def test_one_lesson_per_learner_and_day(sessions: SessionFactory) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")

    def row() -> LessonRow:
        return LessonRow(
            learner_id=learner.id, lesson_date=DAY, topic_id="t", cefr="A2", schema_version=1, status="failed"
        )

    with pytest.raises(IntegrityError), session_scope(sessions) as session:
        session.add_all([row(), row()])
        session.flush()


def test_card_repo_due_returns_due_cards_oldest_first(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    other = LearnerRepo(sessions).get_or_create("43", "A2")
    items = ItemRepo(sessions)
    cards = CardRepo(sessions)
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    def item(slug: str) -> int:
        return items.add(
            key=f"there_is_are:{slug}",
            topic_id="there_is_are",
            cefr="A2",
            form="There is + noun",
            function_ru="сказать, что что-то где-то есть",
            explanation_ru="...",
            sources=lesson.sources,
        ).id

    late = cards.add(learner.id, item("late"), fsrs_state={}, due_at=now - timedelta(days=2))
    soon = cards.add(learner.id, item("soon"), fsrs_state={}, due_at=now - timedelta(hours=1))
    cards.add(learner.id, item("future"), fsrs_state={}, due_at=now + timedelta(days=1))
    cards.add(other.id, item("other"), fsrs_state={}, due_at=now - timedelta(days=5))

    assert [card.id for card in cards.due(learner.id, now)] == [late.id, soon.id]
    assert items.get_by_key("there_is_are:late") is not None
