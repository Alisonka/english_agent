"""Practice after the daily lesson: the quiz an hour later, the translation task in the evening.

`run_practice_tick` runs every few minutes in the server app's scheduler and sends whatever is
due to every learner, on the learner's own schedule. All state lives in `practice_deliveries`: a
block is claimed in the database before it is sent, so a restart or an overlapping tick never
sends it twice. A block that failed half-way stays claimed and is not retried (a duplicate quiz
is worse than a missing one). One learner's failure (a blocked bot, say) never stops the others.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import Settings
from english_agent.items import core_item_from_lesson
from english_agent.logs import bind
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion
from english_agent.models.schedule import local_now
from english_agent.models.storage import LearnerRecord, LessonRecord, PracticeDeliveryRecord, PracticeKind
from english_agent.storage.repositories import PracticeStore, Stores
from english_agent.telegram.client import TelegramClient, TelegramError, TelegramForbiddenError
from english_agent.telegram.render import render_quiz_intro, render_translation_task
from english_agent.tracing import get_tracer

TRANSLATION_PLACEHOLDER = "1. … 2. … 3. …"

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
    lesson = record.lesson
    if record.status != "sent" or record.sent_at is None or lesson is None or not lesson.quiz:
        return None  # a failed day, or a lesson stored before practice existed
    local_time = local_now(learner.timezone, now).time()
    if local_time >= settings.practice_cutoff_time:
        return None
    if quiz is None:
        return "quiz" if now >= record.sent_at + timedelta(minutes=learner.quiz_delay_minutes) else None
    if translation is None and lesson.translation and local_time >= learner.translation_send_time:
        quiz_at = quiz.sent_at or quiz.claimed_at
        if now >= quiz_at + timedelta(minutes=settings.practice_gap_minutes):
            return "translation"
    return None


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


def _send_quiz(
    client: TelegramClient,
    practice: PracticeStore,
    delivery_id: int,
    chat_id: str,
    lesson: Lesson,
    item_id: int,
    rng: random.Random,
) -> list[int]:
    message_ids = [client.send_message(chat_id, render_quiz_intro(lesson))]
    for position, original in enumerate(lesson.quiz):
        question = shuffle_options(original, rng)
        message_id, poll_id = client.send_poll(
            chat_id,
            question.question,
            question.options,
            correct_option=question.correct_option,
            explanation=question.explanation_ru,
        )
        # Recorded right away: the learner may answer before the next poll is sent.
        practice.add_poll(
            delivery_id,
            position=position,
            telegram_poll_id=poll_id,
            telegram_message_id=message_id,
            item_id=item_id,
            question=question,
        )
        message_ids.append(message_id)
    return message_ids


def deliver_practice_for(
    client: TelegramClient,
    settings: Settings,
    stores: Stores,
    learner: LearnerRecord,
    record: LessonRecord,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> PracticeKind | None:
    """Send the block of the learner's lesson `record` that is due at `now`; its kind, or None."""
    lesson = record.lesson
    if lesson is None:
        return None
    quiz = stores.practice.delivery(record.id, "quiz")
    translation = stores.practice.delivery(record.id, "translation")
    kind = due_practice(learner, settings, record, quiz, translation, now)
    if kind is None:
        return None

    # The quiz rates the lesson's core item; created here too, so a lesson stored before items
    # existed (or a failed write at send time) still gets its reviews.
    item, _ = stores.items.get_or_create(core_item_from_lesson(lesson), source_lesson_id=record.id)
    item_count = len(lesson.quiz) if kind == "quiz" else len(lesson.translation)
    delivery = stores.practice.claim_delivery(record.id, kind, item_count)
    if delivery is None:
        return None
    chat_id = learner.telegram_chat_id
    with get_tracer().start_as_current_span("practice.deliver") as span:
        span.set_attribute("practice.kind", kind)
        span.set_attribute("topic.id", lesson.topic_id)
        span.set_attribute("item.key", item.key)
        if kind == "quiz":
            message_ids = _send_quiz(
                client, stores.practice, delivery.id, chat_id, lesson, item.id, rng or random.Random()
            )
        else:
            reply_markup = {"force_reply": True, "input_field_placeholder": TRANSLATION_PLACEHOLDER}
            message_ids = [client.send_message(chat_id, render_translation_task(lesson), reply_markup=reply_markup)]
    stores.practice.mark_sent(delivery.id, message_ids)
    return kind


def deliver_due_practice(
    client: TelegramClient,
    settings: Settings,
    stores: Stores,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> list[tuple[str, PracticeKind]]:
    """Send every learner the practice block that is due at `now`; returns (chat id, kind) of each one sent."""
    learners = stores.learners.list_active()
    today = {learner.id: local_now(learner.timezone, now).date() for learner in learners}
    statuses = stores.lessons.statuses([(learner.id, today[learner.id]) for learner in learners])
    sent: list[tuple[str, PracticeKind]] = []
    for learner in learners:
        if statuses.get((learner.id, today[learner.id])) != "sent":
            continue  # no lesson today yet: nothing to practise
        with bind(chat_id=learner.telegram_chat_id):
            try:
                record = stores.lessons.get(learner.id, today[learner.id])
                if record is None:
                    continue
                kind = deliver_practice_for(client, settings, stores, learner, record, now, rng=rng)
            except TelegramForbiddenError as error:
                log.info("chat blocked the bot, deactivating: %s", error)
                stores.learners.deactivate(learner.telegram_chat_id, "blocked")
                continue
            except (TelegramError, SQLAlchemyError) as error:
                log.warning("practice failed: %s", error)
                continue
            if kind is not None:
                sent.append((learner.telegram_chat_id, kind))
                log.info("practice sent: %s", kind)
    return sent


def run_practice_tick(settings: Settings, stores: Stores) -> None:
    """Scheduler entry point: one pass over all learners, every failure logged (the next tick still runs)."""
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds) as http:
            client = TelegramClient(http, settings.telegram_bot_token.get_secret_value())
            deliver_due_practice(client, settings, stores, datetime.now(UTC))
    except (TelegramError, SQLAlchemyError) as error:
        log.warning("practice tick failed: %s", error)
