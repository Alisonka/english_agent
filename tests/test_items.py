from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

from english_agent.items import backfill_core_items, core_item_from_lesson, core_item_key, record_core_item
from english_agent.models.lesson import Lesson
from fakes import fake_items, fake_stores

DAY = date(2026, 11, 3)


def test_core_item_is_the_lessons_form_function_and_rule(lesson: Lesson) -> None:
    draft = core_item_from_lesson(lesson)

    assert draft.key == core_item_key(lesson.topic_id) == "there_is_are:core"
    assert (draft.topic_id, draft.cefr, draft.form) == (lesson.topic_id, lesson.cefr, lesson.form)
    assert draft.function_ru == lesson.key_idea_ru
    assert draft.explanation_ru == lesson.rule_ru
    assert draft.sources == lesson.sources


async def test_record_core_item_creates_the_item_once(lesson: Lesson) -> None:
    stores, _, _ = fake_stores()

    first = await record_core_item(stores.items, lesson, lesson_id=1)
    second = await record_core_item(stores.items, lesson, lesson_id=2)  # the topic taught again

    assert first is not None and second == first
    assert first.source_lesson_id == 1
    assert list(fake_items(stores).by_key) == ["there_is_are:core"]


async def test_record_core_item_logs_a_storage_failure_instead_of_raising(
    lesson: Lesson, caplog: pytest.LogCaptureFixture
) -> None:
    stores, _, _ = fake_stores()
    fake_items(stores).failure = SQLAlchemyError("connection refused")

    assert await record_core_item(stores.items, lesson, lesson_id=1) is None
    assert "there_is_are" in caplog.text


async def test_backfill_creates_missing_core_items_and_is_idempotent(lesson: Lesson) -> None:
    stores, learners, lessons = fake_stores()
    learner = await learners.get_or_create("42", "A2")
    other = lesson.model_copy(update={"topic_id": "have_got"})
    await lessons.record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    await lessons.record_sent(learner.id, DAY + timedelta(days=1), other, [2], curriculum_version=1)
    await lessons.record_sent(learner.id, DAY + timedelta(days=2), lesson, [3], curriculum_version=1)
    lessons.record_failed(
        learner.id, DAY + timedelta(days=3), topic_id="x", cefr="A2", reason="boom", curriculum_version=1
    )

    first = await backfill_core_items(stores)
    again = await backfill_core_items(stores)

    assert (first.created, first.existing, first.skipped) == (2, 1, 0)
    assert (again.created, again.existing, again.skipped) == (0, 3, 0)
    items = fake_items(stores)
    assert sorted(items.by_key) == ["have_got:core", "there_is_are:core"]
    assert items.by_key["there_is_are:core"].source_lesson_id == 1  # the first lesson stays the source
