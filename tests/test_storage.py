from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from english_agent.items import core_item_from_lesson
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import ItemDraft, LessonRecord
from english_agent.profile import build_profile
from english_agent.srs import AGAIN, GOOD, ReviewScheduler
from english_agent.storage import (
    CardRepo,
    ItemRepo,
    LearnerRepo,
    LessonAlreadySentError,
    LessonContentRepo,
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


async def test_learner_get_or_create_is_idempotent(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    assert await learners.get("42") is None

    created = await learners.get_or_create("42", "A2")
    again = await learners.get_or_create("42", "B1")  # the default only applies to a new learner

    assert again.id == created.id
    assert again.cefr == "A2"
    assert await learners.get("42") == created


async def test_learner_set_cefr_updates_or_creates(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    created = await learners.set_cefr("42", "B1")
    assert created.cefr == "B1"

    updated = await learners.set_cefr("42", "C1")
    assert (updated.id, updated.cefr) == (created.id, "C1")
    assert await learners.get("42") == updated


async def test_lesson_round_trips_through_jsonb(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)

    saved = await lessons.record_sent(learner.id, DAY, lesson, [10, 11], curriculum_version=3)
    loaded = await lessons.get(learner.id, DAY)

    assert isinstance(loaded, LessonRecord)
    assert loaded == saved
    assert loaded.lesson == lesson
    assert loaded.status == "sent"
    assert loaded.telegram_message_ids == [10, 11]
    assert loaded.schema_version == SCHEMA_VERSIONS["lesson"]
    assert loaded.curriculum_version == 3
    assert loaded.sent_at is not None


async def test_failed_lesson_can_be_retried_but_a_sent_one_is_final(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)

    failed = await lessons.record_failed(
        learner.id, DAY, topic_id=lesson.topic_id, cefr="A2", reason="boom", curriculum_version=3
    )
    assert (failed.status, failed.lesson, failed.error) == ("failed", None, "boom")
    assert not await lessons.sent_on(learner.id, DAY)

    sent = await lessons.record_sent(learner.id, DAY, lesson, [1], curriculum_version=3)
    assert sent.id == failed.id
    assert sent.error is None
    assert await lessons.sent_on(learner.id, DAY)
    assert not await lessons.sent_on(learner.id, DAY + timedelta(days=1))

    with pytest.raises(LessonAlreadySentError):
        await lessons.record_sent(learner.id, DAY, lesson, [2], curriculum_version=3)


async def test_one_lesson_per_learner_and_day(sessions: SessionFactory) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")

    def row() -> LessonRow:
        return LessonRow(
            learner_id=learner.id, lesson_date=DAY, topic_id="t", cefr="A2", schema_version=1, status="failed"
        )

    with pytest.raises(IntegrityError):
        async with session_scope(sessions) as session:
            session.add_all([row(), row()])
            await session.flush()


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


async def test_item_get_or_create_keeps_the_first_version(sessions: SessionFactory, lesson: Lesson) -> None:
    items = ItemRepo(sessions)
    assert await items.get_by_key("there_is_are:core") is None

    created, was_created = await items.get_or_create(_draft(lesson), source_lesson_id=7)
    again, was_created_again = await items.get_or_create(
        _draft(lesson).model_copy(update={"form": "changed"}), source_lesson_id=8
    )

    assert (was_created, was_created_again) == (True, False)
    assert again == created
    assert (created.source_lesson_id, created.form, created.version) == (7, "There is + noun", 1)
    assert created.sources == lesson.sources
    assert await items.get_by_key("there_is_are:core") == created


async def test_card_repo_due_returns_due_cards_oldest_first(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    other = await LearnerRepo(sessions).get_or_create("43", "A2")
    items = ItemRepo(sessions)
    cards = CardRepo(sessions, SCHEDULER)
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    async def item(slug: str) -> int:
        return (await items.get_or_create(_draft(lesson, slug)))[0].id

    # "Again" puts the next review one day after the rating, so the rating time sets how overdue a card is.
    late = await cards.review(
        learner.id, await item("late"), AGAIN, now - timedelta(days=5), source="quiz", source_ref="r1"
    )
    soon = await cards.review(
        learner.id, await item("soon"), AGAIN, now - timedelta(days=2), source="quiz", source_ref="r2"
    )
    await cards.review(learner.id, await item("future"), GOOD, now, source="quiz", source_ref="r3")
    await cards.review(other.id, await item("other"), AGAIN, now - timedelta(days=9), source="quiz", source_ref="r4")

    assert [card.id for card in await cards.due(learner.id, now)] == [late.id, soon.id]
    assert late.due_at == now - timedelta(days=4)
    assert (late.reviews, late.last_reviewed_at) == (1, now - timedelta(days=5))
    assert await cards.get(learner.id, late.item_id) == late


async def test_review_is_applied_once_per_source_ref(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    item_id = (await ItemRepo(sessions).get_or_create(_draft(lesson)))[0].id
    cards = CardRepo(sessions, SCHEDULER)
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    first = await cards.review(learner.id, item_id, GOOD, now, source="quiz", source_ref="quiz_answer:1")
    later = now + timedelta(hours=1)
    replay = await cards.review(learner.id, item_id, AGAIN, later, source="quiz", source_ref="quiz_answer:1")

    assert replay == first  # the same event again changes nothing
    assert first.due_at > now and first.reviews == 1
    async with session_scope(sessions) as session:
        logs = (await session.scalars(select(ReviewLogRow))).all()
        assert [(log.rating, log.source, log.source_ref) for log in logs] == [(GOOD, "quiz", "quiz_answer:1")]
        assert logs[0].payload is not None and logs[0].payload["fsrs"]["rating"] == GOOD


# --- practice -------------------------------------------------------------------


async def _quiz_setup(
    sessions: SessionFactory, lesson: Lesson, *, with_item: bool = True
) -> tuple[PracticeRepo, int, int]:
    """A sent quiz; returns the practice repo, the lesson id and the core item id (0 when `with_item` is off)."""
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    record = await LessonRepo(sessions).record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    item_id = (await ItemRepo(sessions).get_or_create(core_item_from_lesson(lesson), source_lesson_id=record.id))[0].id
    practice = PracticeRepo(sessions, SCHEDULER)
    delivery = await practice.claim_delivery(record.id, "quiz", len(lesson.quiz))
    assert delivery is not None
    for position, question in enumerate(lesson.quiz):
        await practice.add_poll(
            delivery.id,
            position=position,
            telegram_poll_id=f"p{position}",
            telegram_message_id=10 + position,
            item_id=item_id if with_item else None,
            question=question,
        )
    await practice.mark_sent(delivery.id, [10, 11, 12])
    return practice, record.id, item_id


async def test_delivery_is_claimed_once(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, _ = await _quiz_setup(sessions, lesson)
    assert await practice.claim_delivery(lesson_id, "quiz", 3) is None
    delivery = await practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.sent_at is not None
    assert delivery.telegram_message_ids == [10, 11, 12]


async def test_poll_answers_complete_the_quiz_and_rate_the_item(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, item_id = await _quiz_setup(sessions, lesson)
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")

    first = await practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    assert first is not None and first.is_correct and not first.completed and first.learner_id == learner.id
    assert await practice.record_poll_answer("p0", "42", wrong_option(lesson, 0)) is None  # repeated answer
    assert await practice.record_poll_answer("unknown", "42", [0]) is None
    await practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    last = await practice.record_poll_answer("p2", "42", [lesson.quiz[2].correct_option])

    assert last is not None and last.completed
    assert [correct for _, correct in last.results] == [True, False, True]
    wrong = lesson.quiz[1]
    async with session_scope(sessions) as session:
        errors = (await session.scalars(select(ErrorRow))).all()
        assert [(e.kind, e.item_id, e.original, e.corrected) for e in errors] == [
            ("quiz", item_id, wrong.options[wrong_option(lesson, 1)[0]], wrong.options[wrong.correct_option])
        ]
        logs = (await session.scalars(select(ReviewLogRow).order_by(ReviewLogRow.id))).all()
        assert [log.rating for log in logs] == [GOOD, AGAIN, GOOD]
        assert all(log.source == "quiz" and log.source_ref.startswith("quiz_answer:") for log in logs)
    card = await CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 3 and card.due_at > datetime.now(UTC)
    delivery = await practice.delivery(lesson_id, "quiz")
    assert delivery is not None and delivery.completed_at is not None


async def test_poll_without_an_item_is_recorded_but_not_rated(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = await _quiz_setup(sessions, lesson, with_item=False)
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")

    result = await practice.record_poll_answer("p0", "42", wrong_option(lesson, 0))

    assert result is not None
    assert await CardRepo(sessions, SCHEDULER).get(learner.id, item_id) is None
    async with session_scope(sessions) as session:
        assert (await session.scalar(select(ErrorRow))).item_id is None


async def test_profile_inputs_and_versions(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = await _quiz_setup(sessions, lesson)
    await practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    await practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    profiles = ProfileRepo(sessions)
    now = datetime.now(UTC)

    inputs = await profiles.inputs(learner.id, now, window_days=7)
    assert (inputs.cefr, inputs.lessons_sent, inputs.quiz_answers, inputs.quiz_correct) == ("A2", 1, 2, 1)
    assert (inputs.cards, inputs.due_cards, inputs.translation_grades) == (1, 0, [])
    assert [error.item_id for error in inputs.errors] == [item_id] and item_id in inputs.items
    assert (
        await profiles.inputs(learner.id, now + timedelta(days=8), window_days=7)
    ).quiz_answers == 0  # window honoured

    assert await profiles.latest(learner.id) is None
    first = await profiles.append(learner.id, build_profile(inputs))
    second = await profiles.append(learner.id, build_profile(inputs))
    assert (first.version, second.version) == (1, 2)
    assert await profiles.latest(learner.id) == second
    assert second.schema_version == SCHEMA_VERSIONS["learner_profile"]
    assert second.profile.weak_points[0].item_id == item_id and second.profile.stats.quiz_accuracy == 0.5


async def test_maintenance_links_old_polls_and_replays_answers(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, _, item_id = await _quiz_setup(sessions, lesson, with_item=False)  # polls from before items existed
    await practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    await practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    maintenance = MaintenanceRepo(sessions, SCHEDULER)

    assert await maintenance.link_polls_to_items() == 3
    assert await maintenance.replay_reviews() == 2
    assert await maintenance.replay_reviews() == 0  # rerun: nothing left to do

    card = await CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 2
    async with session_scope(sessions) as session:
        assert {poll.item_id for poll in (await session.scalars(select(QuizPollRow)))} == {item_id}


async def test_translation_answer_lifecycle(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, item_id = await _quiz_setup(sessions, lesson)
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    assert await practice.open_translation("42") is None  # not sent yet
    task = await practice.claim_delivery(lesson_id, "translation", 3)
    assert task is not None
    await practice.mark_sent(task.id, [20])

    open_task = await practice.open_translation("42")
    assert open_task is not None and open_task.lesson == lesson
    answer = await practice.start_translation_answer(task.id, open_task.learner_id, "first try")
    assert answer is not None
    assert await practice.start_translation_answer(task.id, open_task.learner_id, "again") is None  # pending
    await practice.fail_translation_answer(answer.id, "model is down")
    retry = await practice.start_translation_answer(task.id, open_task.learner_id, "second try")
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
    graded = await practice.finish_translation_answer(answer.id, grade)
    assert graded.status == "graded" and graded.grade == grade
    assert await practice.open_translation("42") is None
    assert await practice.dispute(answer.id) is True
    assert await practice.dispute(999) is False

    # The corrected sentence is an error on the lesson's item; correct and fixed sentences are rated, missing is not.
    async with session_scope(sessions) as session:
        [error] = (await session.scalars(select(ErrorRow))).all()
        assert (error.kind, error.item_id, error.original, error.corrected) == ("text", item_id, "bad", "good")
        logs = (await session.scalars(select(ReviewLogRow).order_by(ReviewLogRow.id))).all()
        assert [(log.rating, log.source_ref) for log in logs] == [
            (GOOD, f"translation_answer:{answer.id}:1"),
            (AGAIN, f"translation_answer:{answer.id}:2"),
        ]
    card = await CardRepo(sessions, SCHEDULER).get(learner.id, item_id)
    assert card is not None and card.reviews == 2


async def test_new_learner_gets_the_given_schedule(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    schedule = LearnerSchedule(timezone="Asia/Vladivostok", lesson_send_time=time(7, 30), quiz_delay_minutes=30)

    created = await learners.get_or_create("42", "A2", schedule)
    assert created.schedule == schedule
    assert (created.is_active, created.paused_at, created.curriculum_started_on) == (True, None, None)
    assert await learners.get_by_id(created.id) == created


async def test_only_started_unpaused_active_learners_are_listed(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    for chat_id in ("1", "2", "3", "4"):
        await learners.get_or_create(chat_id, "A2")
    for chat_id in ("2", "3", "4"):
        await learners.start_course(chat_id, DAY)
    await learners.set_paused("3", True)
    await learners.deactivate("4", "blocked")

    assert [learner.telegram_chat_id for learner in await learners.list_active()] == ["2"]

    await learners.set_paused("3", False)
    restarted = await learners.start_course("4", DAY + timedelta(days=5))
    assert restarted is not None and restarted.curriculum_started_on == DAY  # the first start is kept
    assert restarted.deactivated_reason is None
    assert [learner.telegram_chat_id for learner in await learners.list_active()] == ["2", "3", "4"]


async def test_set_schedule_and_pause_of_an_unknown_chat_return_none(sessions: SessionFactory) -> None:
    learners = LearnerRepo(sessions)
    assert await learners.set_schedule("404", LearnerSchedule()) is None
    assert await learners.set_paused("404", True) is None


async def test_lesson_statuses_and_sent_topics(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)
    await lessons.record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    await lessons.record_failed(
        learner.id, DAY + timedelta(days=1), topic_id="articles", cefr="A2", reason="x", curriculum_version=1
    )

    keys = [(learner.id, DAY), (learner.id, DAY + timedelta(days=1)), (learner.id, DAY + timedelta(days=2))]
    assert await lessons.statuses(keys) == {keys[0]: "sent", keys[1]: "failed"}
    assert await lessons.statuses([]) == {}
    assert await lessons.sent_topic_ids(learner.id, "A2") == {lesson.topic_id}
    assert await lessons.sent_topic_ids(learner.id, "B1") == set()


# --- shared lesson content -----------------------------------------------------------

STALE = timedelta(minutes=20)
AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


async def claim(contents: LessonContentRepo, owner: str, now: datetime, max_attempts: int = 3) -> tuple[str, int, int]:
    outcome, record = await contents.claim(
        "there_is_are", "A2", 1, "key", owner=owner, now=now, stale_after=STALE, max_attempts=max_attempts
    )
    return outcome, record.id, record.attempts


async def test_content_is_written_by_one_claimant_and_then_reused(sessions: SessionFactory, lesson: Lesson) -> None:
    contents = LessonContentRepo(sessions)
    outcome, content_id, attempts = await claim(contents, "a", AT)
    assert (outcome, attempts) == ("claimed", 1)
    assert (await claim(contents, "b", AT + timedelta(minutes=1)))[0] == "busy"

    ready = await contents.finish(content_id, lesson)
    assert (ready.status, ready.lesson, ready.generated_at is not None) == ("ready", lesson, True)
    assert (await claim(contents, "b", AT + timedelta(minutes=2)))[:2] == ("ready", content_id)


async def test_a_dead_claimants_content_is_taken_over(sessions: SessionFactory) -> None:
    contents = LessonContentRepo(sessions)
    await claim(contents, "dead", AT)
    outcome, _, attempts = await claim(contents, "alive", AT + STALE + timedelta(seconds=1))
    assert (outcome, attempts) == ("claimed", 2)


async def test_failed_content_cools_down_then_retries_until_exhausted(sessions: SessionFactory) -> None:
    contents = LessonContentRepo(sessions)
    _, content_id, _ = await claim(contents, "a", AT, max_attempts=2)
    failed = await contents.fail(content_id, "API down")
    assert (failed.status, failed.error) == ("failed", "API down")

    assert (await claim(contents, "b", AT + timedelta(minutes=5), max_attempts=2))[0] == "busy"  # cool-down
    assert (await claim(contents, "b", AT + STALE + timedelta(minutes=1), max_attempts=2))[0] == "claimed"
    await contents.fail(content_id, "API down again")
    assert (await claim(contents, "c", AT + 3 * STALE, max_attempts=2))[0] == "exhausted"
    assert await contents.unavailable_topics("A2", 1, "key", max_attempts=2) == {"there_is_are"}
    assert await contents.unavailable_topics("A2", 1, "other-key", max_attempts=2) == set()


async def test_a_late_failure_does_not_undo_finished_content(sessions: SessionFactory, lesson: Lesson) -> None:
    contents = LessonContentRepo(sessions)
    _, content_id, _ = await claim(contents, "a", AT)
    await contents.finish(content_id, lesson)
    assert (await contents.fail(content_id, "timeout")).status == "ready"


async def test_sent_lesson_points_at_its_content(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    _, content_id, _ = await claim(LessonContentRepo(sessions), "a", AT)
    record = await LessonRepo(sessions).record_sent(
        learner.id, DAY, lesson, [1], curriculum_version=1, content_id=content_id
    )
    assert (record.content_id, record.lesson) == (content_id, lesson)


# --- delivering a day's lesson ---------------------------------------------------------------

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


async def claim_day(lessons: LessonRepo, learner_id: int, now: datetime, max_attempts: int = 3) -> tuple[str, Any]:
    return await lessons.claim(
        learner_id,
        DAY,
        topic_id="there_is_are",
        cefr="A2",
        curriculum_version=1,
        now=now,
        stale_after=timedelta(minutes=10),
        max_attempts=max_attempts,
    )


async def test_a_day_is_claimed_once_sent_in_chunks_and_then_done(sessions: SessionFactory, lesson: Lesson) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)
    outcome, row = await claim_day(lessons, learner.id, NOW)
    assert (outcome, row.status, row.attempts) == ("claimed", "pending", 1)
    assert (await claim_day(lessons, learner.id, NOW + timedelta(minutes=1)))[0] == "busy"

    _, content_id, _ = await claim(LessonContentRepo(sessions), "a", AT)
    await lessons.start_sending(row.id, content_id, lesson)
    await lessons.add_message_id(row.id, 10)
    await lessons.add_message_id(row.id, 11)
    sent = await lessons.finish_sending(row.id)
    assert (sent.status, sent.telegram_message_ids, sent.content_id, sent.lesson) == (
        "sent",
        [10, 11],
        content_id,
        lesson,
    )
    assert (await claim_day(lessons, learner.id, NOW + timedelta(hours=1)))[0] == "done"


async def test_a_dead_senders_day_is_resumed_and_a_failed_one_retried_until_out_of_attempts(
    sessions: SessionFactory, lesson: Lesson
) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)
    _, row = await claim_day(lessons, learner.id, NOW)
    _, content_id, _ = await claim(LessonContentRepo(sessions), "a", AT)
    await lessons.start_sending(row.id, content_id, lesson)
    await lessons.add_message_id(row.id, 10)

    outcome, resumed = await claim_day(lessons, learner.id, NOW + timedelta(minutes=11))
    assert (outcome, resumed.telegram_message_ids, resumed.attempts) == ("resume", [10], 2)

    failed = await lessons.fail(row.id, "telegram: Bad Gateway", final=False)
    assert (failed.status, failed.claimed_at) == ("failed", resumed.claimed_at)  # the backoff counts from here
    assert (await claim_day(lessons, learner.id, NOW + timedelta(minutes=20)))[0] == "claimed"
    assert (await claim_day(lessons, learner.id, NOW + timedelta(minutes=40)))[0] == "done"  # 3 attempts used
    [state] = (await lessons.states([(learner.id, DAY)])).values()
    assert state.status == "skipped"


async def test_a_released_day_does_not_count_an_attempt(sessions: SessionFactory) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)
    _, row = await claim_day(lessons, learner.id, NOW)
    await lessons.release(row.id)
    [state] = (await lessons.states([(learner.id, DAY)])).values()
    assert (state.status, state.claimed_at, state.attempts) == ("pending", None, 0)
    outcome, again = await claim_day(lessons, learner.id, NOW + timedelta(minutes=1))
    assert (outcome, again.attempts) == ("claimed", 1)


async def test_unfinished_past_days_are_closed(sessions: SessionFactory) -> None:
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    lessons = LessonRepo(sessions)
    await claim_day(lessons, learner.id, NOW)
    assert await lessons.close_past(DAY) == 0
    assert await lessons.close_past(DAY + timedelta(days=1)) == 1
    [state] = (await lessons.states([(learner.id, DAY)])).values()
    assert state.status == "skipped"


async def test_grading_attempts_are_counted_and_stale_ones_found(sessions: SessionFactory, lesson: Lesson) -> None:
    practice, lesson_id, _ = await _quiz_setup(sessions, lesson)
    delivery = await practice.claim_delivery(lesson_id, "translation", 3)
    assert delivery is not None
    await practice.mark_sent(delivery.id, [20])
    learner = await LearnerRepo(sessions).get("42")
    assert learner is not None
    answer = await practice.start_translation_answer(delivery.id, learner.id, "my answer")
    assert answer is not None
    assert await practice.delivered_kinds([lesson_id]) == {lesson_id: {"quiz", "translation"}}

    task = await practice.begin_grading(answer.id, NOW)
    assert task is not None and task.answer.grading_attempts == 1 and task.telegram_chat_id == "42"
    assert await practice.stale_gradings(NOW) == []
    assert await practice.stale_gradings(NOW + timedelta(minutes=1)) == [(answer.id, 1)]
    assert await practice.delivered_kinds([1_000_000]) == {}

    await practice.fail_translation_answer(answer.id, "x")
    assert await practice.begin_grading(answer.id, NOW) is None  # not pending any more
