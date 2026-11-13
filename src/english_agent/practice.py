"""Practice after the daily lesson: the quiz an hour later, the translation task in the evening.

`run_practice_tick` runs every few minutes in the server app's scheduler and sends whatever is
due to every learner, on the learner's own schedule. All state lives in `practice_deliveries`: a
block is claimed in the database before it is sent, so a restart or an overlapping tick never
sends it twice. A block that failed half-way stays claimed and is not retried (a duplicate quiz
is worse than a missing one). One learner's failure (a blocked bot, say) never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timedelta

from aiogram.types import ForceReply
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import Settings
from english_agent.items import core_item_from_lesson, step_item_from
from english_agent.logs import bind
from english_agent.models.practice import QuizQuestion
from english_agent.models.schedule import local_now
from english_agent.models.storage import (
    LearnerRecord,
    LessonRecord,
    PracticeBlock,
    PracticeDeliveryRecord,
    PracticeKind,
)
from english_agent.planning import practice_due
from english_agent.practice_select import (
    StepBank,
    assemble_quiz,
    assemble_translation,
    bank_from_lesson,
    bank_from_record,
    bank_from_step,
    choose_previous,
)
from english_agent.srs import make_scheduler
from english_agent.storage.repositories import PracticeStore, Stores
from english_agent.telegram.render import render_quiz_intro, render_translation_task
from english_agent.telegram.sender import Sender, TelegramError, TelegramForbiddenError
from english_agent.tracing import get_tracer

TRANSLATION_PLACEHOLDER = "1. … 2. … 3. …"
_CONCURRENT_LEARNERS = 16  # sends are rate-limited anyway; this bounds open DB transactions

log = logging.getLogger(__name__)


def due_practice(
    learner: LearnerRecord,
    settings: Settings,
    record: LessonRecord,
    quiz: PracticeDeliveryRecord | None,
    translation: PracticeDeliveryRecord | None,
    now: datetime,
) -> PracticeKind | None:
    """Which block of today's lesson should be sent at `now`, if any.

    The learner's own time zone, quiz delay and translation time; the cutoff and the gap
    between the quiz and the translation task are the same for everyone.
    """
    if record.status != "sent" or record.sent_at is None or not _has_practice(record):
        return None  # a failed day, or a lesson stored before practice existed
    local_time = local_now(learner.timezone, now).time()
    if local_time >= settings.practice_cutoff_time:
        return None
    if quiz is None:
        return "quiz" if now >= record.sent_at + timedelta(minutes=learner.quiz_delay_minutes) else None
    if translation is None and local_time >= learner.translation_send_time:
        quiz_at = quiz.sent_at or quiz.claimed_at
        if now >= quiz_at + timedelta(minutes=settings.practice_gap_minutes):
            return "translation"
    return None


def _has_practice(record: LessonRecord) -> bool:
    """A step day always has practice (from its bank); a legacy day needs a stored quiz."""
    if record.payload_kind == "step":
        return True
    lesson = record.lesson
    return lesson is not None and bool(lesson.quiz)


def shuffle_options(question: QuizQuestion, rng: random.Random) -> QuizQuestion:
    """The question with its options in random order (the writer tends to put the answer in one place)."""
    order = list(range(len(question.options)))
    rng.shuffle(order)
    return question.model_copy(
        update={
            "options": [question.options[index] for index in order],
            "correct_option": order.index(question.correct_option),
        }
    )


async def _send_quiz(
    sender: Sender,
    practice: PracticeStore,
    delivery_id: int,
    chat_id: str,
    title_en: str,
    block: PracticeBlock,
    rng: random.Random,
) -> list[int]:
    message_ids = [await sender.send_text(chat_id, render_quiz_intro(title_en, len(block.questions)))]
    for position, chosen in enumerate(block.questions):
        question = shuffle_options(chosen.question, rng)
        message_id, poll_id = await sender.send_poll(
            chat_id,
            question.question,
            question.options,
            correct_option=question.correct_option,
            explanation=question.explanation_ru,
        )
        # Recorded right away: the learner may answer before the next poll is sent.
        await practice.add_poll(
            delivery_id,
            position=position,
            telegram_poll_id=poll_id,
            telegram_message_id=message_id,
            item_id=chosen.item_id,
            question=question,
        )
        message_ids.append(message_id)
    return message_ids


async def _banks(
    settings: Settings, stores: Stores, learner: LearnerRecord, record: LessonRecord, now: datetime
) -> tuple[StepBank, StepBank | None, StepBank | None] | None:
    """Today's bank, the earlier-step bank (least recalled) and the older-topic review bank."""
    scheduler = make_scheduler(settings)
    if record.payload_kind == "step":
        content = await stores.contents.get(record.content_id) if record.content_id is not None else None
        topic_content = content.topic if content is not None else None
        if topic_content is None:
            return None
        index = record.step_index
        main = topic_content.step(index, "main")
        today_item, _ = await stores.items.get_or_create(
            step_item_from(main, topic_content.form_for(index)), source_lesson_id=record.id
        )
        today_bank = bank_from_step(main, today_item.id)
        earlier = [topic_content.step(i, "main") for i in range(1, index)]
        id_by_key = await stores.items.ids_for_keys([step.item_key for step in earlier])
        cards = {card.item_id: card for card in await stores.cards.for_items(learner.id, list(id_by_key.values()))}
        banks: list[StepBank] = []
        retrievability: dict[int, float] = {}
        for step in earlier:
            item_id = id_by_key.get(step.item_key)
            banks.append(bank_from_step(step, item_id))
            card = cards.get(item_id) if item_id is not None else None
            if item_id is not None and card is not None:
                retrievability[item_id] = scheduler.retrievability(card.fsrs_state, now)
        previous_bank = choose_previous(banks, retrievability) if banks else None
    else:
        lesson = record.lesson
        if lesson is None:
            return None
        today_item, _ = await stores.items.get_or_create(core_item_from_lesson(lesson), source_lesson_id=record.id)
        today_bank = bank_from_lesson(lesson, record.item_key, today_item.id)
        previous_bank = None

    review_bank = None
    chosen = await stores.cards.due_or_weakest(learner.id, now, exclude_topic_id=record.topic_id)
    if chosen is not None:
        _, item = chosen
        taught = await stores.lessons.taught_step(learner.id, item.key)
        if taught is not None:
            review_bank = bank_from_record(taught, item.id)
    return today_bank, previous_bank, review_bank


async def deliver_practice_for(
    sender: Sender,
    settings: Settings,
    stores: Stores,
    learner: LearnerRecord,
    record: LessonRecord,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> PracticeKind | None:
    """Send the block of the learner's lesson `record` that is due at `now`; its kind, or None."""
    if not _has_practice(record):
        return None
    quiz = await stores.practice.delivery(record.id, "quiz")
    translation = await stores.practice.delivery(record.id, "translation")
    kind = due_practice(learner, settings, record, quiz, translation, now)
    if kind is None:
        return None
    banks = await _banks(settings, stores, learner, record, now)
    if banks is None:
        return None
    today_bank, previous_bank, review_bank = banks
    rng = rng or random.Random()
    if kind == "quiz":
        block = assemble_quiz(today_bank, previous_bank, review_bank, rng=rng)
        item_count = len(block.questions)
    else:
        other = previous_bank or review_bank
        block = assemble_translation(
            today_bank, other, other_source="previous" if previous_bank is not None else "review", rng=rng
        )
        item_count = len(block.sentences)
    delivery = await stores.practice.claim_delivery(record.id, kind, item_count, payload=block.model_dump(mode="json"))
    if delivery is None:
        return None
    chat_id = learner.telegram_chat_id
    with get_tracer().start_as_current_span("practice.deliver") as span:
        span.set_attribute("practice.kind", kind)
        span.set_attribute("topic.id", record.topic_id)
        span.set_attribute("item.key", today_bank.item_key)
        if kind == "quiz":
            message_ids = await _send_quiz(
                sender, stores.practice, delivery.id, chat_id, today_bank.title_en, block, rng
            )
        else:
            reply_markup = ForceReply(force_reply=True, input_field_placeholder=TRANSLATION_PLACEHOLDER)
            text = render_translation_task(today_bank.title_en, [sentence.task for sentence in block.sentences])
            message_ids = [await sender.send_text(chat_id, text, reply_markup=reply_markup)]
    await stores.practice.mark_sent(delivery.id, message_ids)
    return kind


async def deliver_practice_to(
    sender: Sender,
    settings: Settings,
    stores: Stores,
    learner: LearnerRecord,
    day: date,
    now: datetime,
    rng: random.Random | None = None,
) -> PracticeKind | None:
    """One learner's due practice block of `day`, every failure handled: a job's whole body."""
    with bind(chat_id=learner.telegram_chat_id):
        try:
            record = await stores.lessons.get(learner.id, day)
            if record is None:
                return None
            kind = await deliver_practice_for(sender, settings, stores, learner, record, now, rng=rng)
        except TelegramForbiddenError as error:
            log.info("chat blocked the bot, deactivating: %s", error)
            await stores.learners.deactivate(learner.telegram_chat_id, "blocked")
            return None
        except (TelegramError, SQLAlchemyError) as error:
            log.warning("practice failed: %s", error)
            return None
        if kind is not None:
            log.info("practice sent: %s", kind)
        return kind


async def deliver_due_practice(
    sender: Sender,
    settings: Settings,
    stores: Stores,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> list[tuple[str, PracticeKind]]:
    """Send, in this process, every learner the practice block due at `now` (local runs and tests).

    The server does the same through the job queue: the scheduler enqueues one `deliver_practice`
    job per learner `planning.practice_due` lets through. Returns (chat id, kind) of each block sent.
    """
    learners = await stores.learners.list_active()
    today = {learner.id: local_now(learner.timezone, now).date() for learner in learners}
    states = await stores.lessons.states([(learner.id, today[learner.id]) for learner in learners])
    claimed = await stores.practice.delivered_kinds([state.lesson_id for state in states.values()])
    due = []
    for learner in learners:
        state = states.get((learner.id, today[learner.id]))
        kinds = claimed.get(state.lesson_id, set()) if state is not None else set()
        if practice_due(learner, state, kinds, now, settings):
            due.append(learner)
    gate = asyncio.Semaphore(_CONCURRENT_LEARNERS)

    async def one(learner: LearnerRecord) -> PracticeKind | None:
        async with gate:
            return await deliver_practice_to(sender, settings, stores, learner, today[learner.id], now, rng)

    kinds_sent = await asyncio.gather(*(one(learner) for learner in due))
    return [(learner.telegram_chat_id, kind) for learner, kind in zip(due, kinds_sent, strict=True) if kind is not None]
