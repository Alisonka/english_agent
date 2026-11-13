"""Lessons as learnable items: the unit that reviews, cards and errors attach to.

Every sent lesson yields one *core item*, `<topic_id>:core`: the topic's form, its one-line
function and the full rule, with the lesson's sources. Finer-grained items (one per recurring
error) come later from the Reflector. A topic's core item is created once: teaching the topic
again keeps the original, so cards and errors never lose their anchor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import trace
from sqlalchemy.exc import SQLAlchemyError

from english_agent.models.lesson import Lesson
from english_agent.models.storage import ItemDraft, ItemRecord, core_item_key
from english_agent.models.topic import LessonStep
from english_agent.storage.repositories import ItemStore, Stores

__all__ = [
    "backfill_core_items",
    "core_item_from_lesson",
    "core_item_key",
    "record_core_item",
    "step_item_from",
]

log = logging.getLogger(__name__)


def core_item_from_lesson(lesson: Lesson) -> ItemDraft:
    return ItemDraft(
        key=core_item_key(lesson.topic_id),
        topic_id=lesson.topic_id,
        cefr=lesson.cefr,
        form=lesson.form,
        function_ru=lesson.key_idea_ru,
        explanation_ru=lesson.rule_ru,
        sources=lesson.sources,
    )


def step_item_from(step: LessonStep, form: str | None) -> ItemDraft:
    """The learnable item for a step: keyed `<topic>:<slug>`, with the form in force at this step."""
    return ItemDraft(
        key=step.item_key,
        topic_id=step.topic_id,
        cefr=step.cefr,
        form=form or step.message.form or "—",
        function_ru=step.message.key_idea_ru,
        explanation_ru=step.message.key_idea_ru,
        sources=step.sources,
    )


async def record_core_item(items: ItemStore, lesson: Lesson, lesson_id: int) -> ItemRecord | None:
    """Make sure the lesson's topic has its core item; a storage failure is logged, never raised.

    Runs after the lesson is sent and recorded: the learner already has it, so a failure here
    must not turn the day into a failed one. `python -m english_agent.storage backfill-items`
    fills any gap later.
    """
    span = trace.get_current_span()
    try:
        item, created = await items.get_or_create(core_item_from_lesson(lesson), source_lesson_id=lesson_id)
    except SQLAlchemyError as error:
        span.record_exception(error)
        log.warning("could not record the item for %s: %s", lesson.topic_id, error)
        return None
    span.set_attribute("item.key", item.key)
    span.set_attribute("item.created", created)
    return item


@dataclass
class BackfillResult:
    created: int = 0
    existing: int = 0
    skipped: int = 0  # sent lessons without a stored payload


async def backfill_core_items(stores: Stores) -> BackfillResult:
    """An item for every sent day that lacks one: a step item for step rows, a core item for legacy
    lessons. Idempotent: safe to run again any time."""
    result = BackfillResult()
    for record in await stores.lessons.list_sent():
        step, lesson = record.step, record.lesson
        if step is not None:
            draft = step_item_from(step, step.message.form)
        elif lesson is not None:
            draft = core_item_from_lesson(lesson)
        else:
            result.skipped += 1
            continue
        _, created = await stores.items.get_or_create(draft, source_lesson_id=record.id)
        if created:
            result.created += 1
        else:
            result.existing += 1
    return result
