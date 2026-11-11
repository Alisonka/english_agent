from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from english_agent.items import core_item_from_lesson
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import ItemDraft, LessonRecord
from english_agent.profile import build_profile
from english_agent.srs import AGAIN, GOOD, ReviewScheduler
from english_agent.storage import (
    CardRepo,
    ItemRepo,
    LearnerRepo,
    LessonAlreadySentError,
    LessonRepo,
    MaintenanceRepo,
    PracticeRepo,
    ProfileRepo,
    SessionFactory,
    session_scope,
)
from english_agent.storage.orm import ErrorRow, LessonRow, QuizPollRow, ReviewLogRow
from english_agent.versions import SCHEMA_VERSIONS

DAY = date(2026, 11, 3)
SCHEDULER = ReviewScheduler(enable_fuzzing=False)


def wrong_option(lesson: Lesson, position: int) -> list[int]:
    question = lesson.quiz[position]
    return [(question.correct_option + 1) % len(question.options)]


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


def _draft(lesson: Lesson, slug: str = "core") -> ItemDraft:
    return ItemDraft(
        key=f"there_is_are:{slug}",
        topic_id="there_is_are",
        cefr="A2",
        form="There is + noun",
        function_ru="сказать, что что-то где-то есть",
        explanation_ru="...",
        sources=lesson.sources,
    )


def test_item_get_or_create_keeps_the_first_version(sessions: SessionFactory, lesson: Lesson) -> None:
    items = ItemRepo(sessions)
    assert items.get_by_key("there_is_are:core") is None

    created, was_created = items.get_or_create(_draft(lesson), source_lesson_id=7)
    again, was_created_again = items.get_or_create(
        _draft(lesson).model_copy(update={"form": "changed"}), source_lesson_id=8
    )

    assert (was_created, was_created_again) == (True, False)
    assert again == created
    assert (created.source_lesson_id, created.form, created.version) == (7, "There is + noun", 1)
    assert created.sources == lesson.sources
    assert items.get_by_key("there_is_are:core") == created


def test_card_repo_due_returns_due_cards_oldest_first(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    other = LearnerRepo(sessions).get_or_create("43", "A2")
    items = ItemRepo(sessions)
    cards = CardRepo(sessions, SCHEDULER)
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    def item(slug: str) -> int:
        return items.get_or_create(_draft(lesson, slug))[0].id

    # "Again" puts the next review one day after the rating, so the rating time sets how overdue a card is.
    late = cards.review(learner.id, item("late"), AGAIN, now - timedelta(days=5), source="quiz", source_ref="r1")
    soon = cards.review(learner.id, item("soon"), AGAIN, now - timedelta(days=2), source="quiz", source_ref="r2")
    cards.review(learner.id, item("future"), GOOD, now, source="quiz", source_ref="r3")
    cards.review(other.id, item("other"), AGAIN, now - timedelta(days=9), source="quiz", source_ref="r4")

    assert [card.id for card in cards.due(learner.id, now)] == [late.id, soon.id]
    assert late.due_at == now - timedelta(days=4)
    assert (late.reviews, late.last_reviewed_at) == (1, now - timedelta(days=5))
    assert cards.get(learner.id, late.item_id) == late


def test_review_is_applied_once_per_source_ref(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    item_id = ItemRepo(sessions).get_or_create(_draft(lesson))[0].id
    cards = CardRepo(sessions, SCHEDULER)
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    first = cards.review(learner.id, item_id, GOOD, now, source="quiz", source_ref="quiz_answer:1")
    later = now + timedelta(hours=1)
    replay = cards.review(learner.id, item_id, AGAIN, later, source="quiz", source_ref="quiz_answer:1")

    assert replay == first  # the same event again changes nothing
    assert first.due_at > now and first.reviews == 1
    with session_scope(sessions) as session:
        logs = session.scalars(select(ReviewLogRow)).all()
        assert [(log.rating, log.source, log.source_ref) for log in logs] == [(GOOD, "quiz", "quiz_answer:1")]
        assert logs[0].payload is not None and logs[0].payload["fsrs"]["rating"] == GOOD


# --- practice -------------------------------------------------------------------


def _quiz_setup(sessions: SessionFactory, lesson: Lesson, *, with_item: bool = True) -> tuple[PracticeRepo, int, int]:
    """A sent quiz; returns the practice repo, the lesson id and the core item id (0 when `with_item` is off)."""
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    record = LessonRepo(sessions).record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    item_id = ItemRepo(sessions).get_or_create(core_item_from_lesson(lesson), source_lesson_id=record.id)[0].id
    practice = PracticeRepo(sessions, SCHEDULER)
    delivery = practice.claim_delivery(record.id, "quiz", len(lesson.quiz))
    assert delivery is not None
    for position, question in enumerate(lesson.quiz):
        practice.add_poll(
            delivery.id,
            position=position,
            telegram_poll_id=f"p{position}",
            telegram_message_id=10 + position,
            item_id=item_id if with_item else None,
            question=question,
        )
    practice.mark_sent(delivery.id, [10, 11, 12])
    return practice, record.id, item_id


def test_delivery_is_claimed_once(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, _ = _quiz_setup(sessions, lesson)
    assert practice.claim_delivery(lesson_id, "quiz", 3) is None
    delivery = practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.sent_at is not None
    assert delivery.telegram_message_ids == [10, 11, 12]


def test_poll_answers_complete_the_quiz_and_rate_the_item(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, item_id = _quiz_setup(sessions, lesson)
    learner = LearnerRepo(sessions).get_or_create("42", "A2")

    first = practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    assert first is not None and first.is_correct and not first.completed and first.learner_id == learner.id
    assert practice.record_poll_answer("p0", "42", wrong_option(lesson, 0)) is None  # repeated answer
    assert practice.record_poll_answer("unknown", "42", [0]) is None
    practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    last = practice.record_poll_answer("p2", "42", [lesson.quiz[2].correct_option])

    assert last is not None and last.completed
    assert [correct for _, correct in last.results] == [True, False, True]
    wrong = lesson.quiz[1]
    with session_scope(sessions) as session:
        errors = session.scalars(select(ErrorRow)).all()
        assert [(e.kind, e.item_id, e.original, e.corrected) for e in errors] == [
            ("quiz", item_id, wrong.options[wrong_option(lesson, 1)[0]], wrong.options[wrong.correct_option])
        ]
        logs = session.scalars(select(ReviewLogRow).order_by(ReviewLogRow.id)).all()
        assert [log.rating for log in logs] == [GOOD, AGAIN, GOOD]
        assert all(log.source == "quiz" and log.source_ref.startswith("quiz_answer:") for log in logs)
    card = CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 3 and card.due_at > datetime.now(UTC)
    delivery = practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.completed_at is not None


def test_poll_without_an_item_is_recorded_but_not_rated(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = _quiz_setup(sessions, lesson, with_item=False)
    learner = LearnerRepo(sessions).get_or_create("42", "A2")

    result = practice.record_poll_answer("p0", "42", wrong_option(lesson, 0))

    assert result is not None
    assert CardRepo(sessions, SCHEDULER).get(learner.id, item_id) is None
    with session_scope(sessions) as session:
        assert session.scalar(select(ErrorRow)).item_id is None


def test_profile_inputs_and_versions(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = _quiz_setup(sessions, lesson)
    practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    profiles = ProfileRepo(sessions)
    now = datetime.now(UTC)

    inputs = profiles.inputs(learner.id, now, window_days=7)
    assert (inputs.cefr, inputs.lessons_sent, inputs.quiz_answers, inputs.quiz_correct) == ("A2", 1, 2, 1)
    assert (inputs.cards, inputs.due_cards, inputs.translation_grades) == (1, 0, [])
    assert [error.item_id for error in inputs.errors] == [item_id] and item_id in inputs.items
    assert profiles.inputs(learner.id, now + timedelta(days=8), window_days=7).quiz_answers == 0  # window honoured

    assert profiles.latest(learner.id) is None
    first = profiles.append(learner.id, build_profile(inputs))
    second = profiles.append(learner.id, build_profile(inputs))
    assert (first.version, second.version) == (1, 2)
    assert profiles.latest(learner.id) == second
    assert second.schema_version == SCHEMA_VERSIONS["learner_profile"]
    assert second.profile.weak_points[0].item_id == item_id and second.profile.stats.quiz_accuracy == 0.5


def test_maintenance_links_old_polls_and_replays_answers(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = _quiz_setup(sessions, lesson, with_item=False)  # polls from before items existed
    practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
    maintenance = MaintenanceRepo(sessions, SCHEDULER)

    assert maintenance.link_polls_to_items() == 3
    assert maintenance.replay_reviews() == 2
    assert maintenance.replay_reviews() == 0  # rerun: nothing left to do

    card = CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 2
    with session_scope(sessions) as session:
        assert {poll.item_id for poll in session.scalars(select(QuizPollRow))} == {item_id}


def test_translation_answer_lifecycle(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, item_id = _quiz_setup(sessions, lesson)
    learner = LearnerRepo(sessions).get_or_create("42", "A2")
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

    # The corrected sentence is an error on the lesson's item; correct and fixed sentences are rated, missing is not.
    with session_scope(sessions) as session:
        [error] = session.scalars(select(ErrorRow)).all()
        assert (error.kind, error.item_id, error.original, error.corrected) == ("text", item_id, "bad", "good")
        logs = session.scalars(select(ReviewLogRow).order_by(ReviewLogRow.id)).all()
        assert [(log.rating, log.source_ref) for log in logs] == [
            (GOOD, f"translation_answer:{answer.id}:1"),
            (AGAIN, f"translation_answer:{answer.id}:2"),
        ]
    card = CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 2
