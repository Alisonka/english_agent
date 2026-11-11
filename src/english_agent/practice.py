"""Practice after the daily lesson: the quiz an hour later, the translation task in the evening.

`run_practice_tick` runs every few minutes in the server app's scheduler and sends whatever is
due. All state lives in `practice_deliveries`: a block is claimed in the database before it is
sent, so a restart or an overlapping tick never sends it twice. A block that failed half-way
stays claimed and is not retried (a duplicate quiz is worse than a missing one).
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import Settings
from english_agent.items import core_item_from_lesson
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion
from english_agent.models.storage import LessonRecord, PracticeDeliveryRecord, PracticeKind
from english_agent.storage.repositories import PracticeStore, Stores
from english_agent.telegram.client import TelegramClient, TelegramError
from english_agent.telegram.render import render_quiz_intro, render_translation_task
from english_agent.tracing import get_tracer

TRANSLATION_PLACEHOLDER = "1. … 2. … 3. …"


def due_practice(
    settings: Settings,
    record: LessonRecord,
    quiz: PracticeDeliveryRecord | None,
    translation: PracticeDeliveryRecord | None,
    now: datetime,
) -> PracticeKind | None:
    """Which block of today's lesson should be sent at `now`, if any."""
    lesson = record.lesson
    if record.status != "sent" or record.sent_at is None or lesson is None or not lesson.quiz:
        return None  # a failed day, or a lesson stored before practice existed
    local_time = now.astimezone(ZoneInfo(settings.timezone)).time()
    if local_time >= settings.practice_cutoff_time:
        return None
    if quiz is None:
        return "quiz" if now >= record.sent_at + timedelta(minutes=settings.quiz_delay_minutes) else None
    if translation is None and lesson.translation and local_time >= settings.translation_send_time:
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


def deliver_due_practice(
    client: TelegramClient,
    settings: Settings,
    stores: Stores,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> PracticeKind | None:
    """Send the block of today's lesson that is due at `now`; returns its kind, or None if nothing was sent."""
    learner = stores.learners.get(settings.telegram_chat_id)
    if learner is None:
        return None
    today = now.astimezone(ZoneInfo(settings.timezone)).date()
    record = stores.lessons.get(learner.id, today)
    if record is None or record.lesson is None:
        return None
    quiz = stores.practice.delivery(record.id, "quiz")
    translation = stores.practice.delivery(record.id, "translation")
    kind = due_practice(settings, record, quiz, translation, now)
    if kind is None:
        return None

    lesson = record.lesson
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


def run_practice_tick(settings: Settings, stores: Stores) -> None:
    """Scheduler entry point: one check, every failure logged (the next tick still runs)."""
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds) as http:
            client = TelegramClient(http, settings.telegram_bot_token.get_secret_value())
            kind = deliver_due_practice(client, settings, stores, datetime.now(UTC))
    except (TelegramError, SQLAlchemyError) as error:
        print(f"practice tick failed: {error}", file=sys.stderr)
        return
    if kind is not None:
        print(f"practice sent: {kind}", file=sys.stderr)
