from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import LessonRecord
from english_agent.storage import (
    CardRepo,
    ItemRepo,
    LearnerRepo,
    LessonAlreadySentError,
    LessonRepo,
    PracticeRepo,
    SessionFactory,
    session_scope,
)
from english_agent.storage.orm import ErrorRow, LessonRow
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


# --- practice -------------------------------------------------------------------


def _quiz_setup(sessions: SessionFactory, lesson: Lesson) -> tuple[PracticeRepo, int]:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    record = LessonRepo(sessions).record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    practice = PracticeRepo(sessions)
    delivery = practice.claim_delivery(record.id, "quiz", len(lesson.quiz))
    assert delivery is not None
    for position, question in enumerate(lesson.quiz):
        practice.add_poll(
            delivery.id,
            position=position,
            telegram_poll_id=f"p{position}",
            telegram_message_id=10 + position,
            question=question,
        )
    practice.mark_sent(delivery.id, [10, 11, 12])
    return practice, record.id


def test_delivery_is_claimed_once(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id = _quiz_setup(sessions, lesson)
    assert practice.claim_delivery(lesson_id, "quiz", 3) is None
    delivery = practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.sent_at is not None
    assert delivery.telegram_message_ids == [10, 11, 12]


def test_poll_answers_complete_the_quiz_and_log_errors(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id = _quiz_setup(sessions, lesson)

    first = practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    assert first is not None and first.is_correct and not first.completed
    assert practice.record_poll_answer("p0", "42", [0]) is None  # repeated answer
    assert practice.record_poll_answer("unknown", "42", [0]) is None
    practice.record_poll_answer("p1", "42", [0])
    last = practice.record_poll_answer("p2", "42", [lesson.quiz[2].correct_option])

    assert last is not None and last.completed
    assert [correct for _, correct in last.results] == [True, False, True]
    with session_scope(sessions) as session:
        errors = session.scalars(select(ErrorRow)).all()
        assert [(e.kind, e.original, e.corrected) for e in errors] == [
            ("quiz", lesson.quiz[1].options[0], lesson.quiz[1].options[lesson.quiz[1].correct_option])
        ]
    delivery = practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.completed_at is not None


def test_translation_answer_lifecycle(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id = _quiz_setup(sessions, lesson)
    assert practice.open_translation("42") is None  # not sent yet
    task = practice.claim_delivery(lesson_id, "translation", 3)
    assert task is not None
    practice.mark_sent(task.id, [20])

    open_task = practice.open_translation("42")
    assert open_task is not None and open_task.lesson == lesson
    answer = practice.start_translation_answer(task.id, open_task.learner_id, "first try")
    assert answer is not None
    assert practice.start_translation_answer(task.id, open_task.learner_id, "again") is None  # pending
    practice.fail_translation_answer(answer.id, "model is down")
    retry = practice.start_translation_answer(task.id, open_task.learner_id, "second try")
    assert retry is not None and retry.id == answer.id and retry.answer_text == "second try"

    grade = TranslationGrade.model_validate(
        {
            "sentences": [
                {"n": 1, "learner_text": "ok", "verdict": "correct", "corrected": "ok", "explanation_ru": ""},
                {"n": 2, "learner_text": "bad", "verdict": "fixed", "corrected": "good", "explanation_ru": "Так надо."},
                {"n": 3, "learner_text": "", "verdict": "missing", "corrected": "ref", "explanation_ru": ""},
            ],
            "focus_ru": "Следи за формой.",
        }
    )
    graded = practice.finish_translation_answer(answer.id, grade)
    assert graded.status == "graded" and graded.grade == grade
    assert practice.open_translation("42") is None
    assert practice.dispute(answer.id) is True
    assert practice.dispute(999) is False
