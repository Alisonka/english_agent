from __future__ import annotations

import json
import random
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from english_agent.config import Settings
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LessonRecord, PracticeDeliveryRecord
from english_agent.practice import deliver_due_practice, due_practice, shuffle_options
from english_agent.storage import Stores
from english_agent.telegram import TelegramClient
from fakes import fake_items, fake_practice, fake_stores

MOSCOW = ZoneInfo("Europe/Moscow")
DAY = date(2026, 11, 6)
LESSON_AT = datetime(2026, 11, 6, 9, 0, tzinfo=MOSCOW)


def settings() -> Settings:
    return Settings(_env_file=None, telegram_chat_id="42", timezone="Europe/Moscow")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=MOSCOW)


def sent_lesson(lesson: Lesson, sent_at: datetime = LESSON_AT) -> LessonRecord:
    return LessonRecord(
        id=1,
        learner_id=1,
        lesson_date=DAY,
        topic_id=lesson.topic_id,
        cefr=lesson.cefr,
        curriculum_version=1,
        schema_version=3,
        status="sent",
        lesson=lesson,
        error=None,
        telegram_message_ids=[1],
        created_at=sent_at,
        sent_at=sent_at,
    )


def delivery(kind: str, sent_at: datetime) -> PracticeDeliveryRecord:
    return PracticeDeliveryRecord.model_validate(
        {
            "id": 1,
            "lesson_id": 1,
            "kind": kind,
            "item_count": 3,
            "claimed_at": sent_at,
            "sent_at": sent_at,
            "telegram_message_ids": [],
            "completed_at": None,
        }
    )


# --- when is what due ---------------------------------------------------------


def test_quiz_is_due_an_hour_after_the_lesson(lesson: Lesson) -> None:
    record = sent_lesson(lesson)
    assert due_practice(settings(), record, None, None, at(9, 59)) is None
    assert due_practice(settings(), record, None, None, at(10, 0)) == "quiz"


def test_translation_waits_for_evening_and_for_the_quiz(lesson: Lesson) -> None:
    record = sent_lesson(lesson)
    quiz = delivery("quiz", at(10))
    assert due_practice(settings(), record, quiz, None, at(17, 55)) is None
    assert due_practice(settings(), record, quiz, None, at(18, 0)) == "translation"
    assert due_practice(settings(), record, quiz, delivery("translation", at(18)), at(18, 5)) is None


def test_late_lesson_shifts_the_whole_chain(lesson: Lesson) -> None:
    record = sent_lesson(lesson, sent_at=at(17, 30))
    assert due_practice(settings(), record, None, None, at(18, 0)) is None
    assert due_practice(settings(), record, None, None, at(18, 30)) == "quiz"
    quiz = delivery("quiz", at(18, 30))
    assert due_practice(settings(), record, quiz, None, at(19, 0)) is None
    assert due_practice(settings(), record, quiz, None, at(19, 30)) == "translation"


def test_nothing_is_sent_after_the_cutoff(lesson: Lesson) -> None:
    record = sent_lesson(lesson, sent_at=at(21, 0))
    assert due_practice(settings(), record, None, None, at(22, 0)) is None


def test_lesson_without_practice_gets_none(lesson: Lesson) -> None:
    record = sent_lesson(lesson.model_copy(update={"quiz": [], "translation": []}))
    assert due_practice(settings(), record, None, None, at(12)) is None


def test_shuffle_keeps_the_correct_answer(lesson: Lesson) -> None:
    original = lesson.quiz[0]
    for seed in range(20):
        shuffled = shuffle_options(original, random.Random(seed))
        assert sorted(shuffled.options) == sorted(original.options)
        assert shuffled.options[shuffled.correct_option] == original.options[original.correct_option]


# --- sending --------------------------------------------------------------------


class PollTelegram:
    """Answers sendMessage and sendPoll like the Bot API; records every call."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((method, json.loads(request.content or b"{}")))
        message_id = len(self.requests)
        result: dict[str, Any] = {"message_id": message_id}
        if method == "sendPoll":
            result["poll"] = {"id": f"poll-{message_id}"}
        return httpx.Response(200, json={"ok": True, "result": result})

    def client(self) -> TelegramClient:
        return TelegramClient(httpx.Client(transport=httpx.MockTransport(self._handler)), "1:T")


@pytest.fixture
def stores(lesson: Lesson) -> Stores:
    stores, learners, lessons = fake_stores()
    learner = learners.get_or_create("42", "A2")
    record = lessons.record_sent(learner.id, DAY, lesson, [1], curriculum_version=1)
    lessons.rows[(learner.id, DAY)] = record.model_copy(update={"sent_at": LESSON_AT})
    return stores


def test_quiz_is_sent_once_and_recorded(stores: Stores, lesson: Lesson) -> None:
    telegram = PollTelegram()
    now = at(11)

    assert deliver_due_practice(telegram.client(), settings(), stores, now, rng=random.Random(1)) == "quiz"
    assert deliver_due_practice(telegram.client(), settings(), stores, now) is None  # already claimed

    methods = [method for method, _ in telegram.requests]
    assert methods == ["sendMessage", "sendPoll", "sendPoll", "sendPoll"]
    poll = telegram.requests[1][1]
    assert poll["type"] == "quiz" and poll["is_anonymous"] is False
    practice = fake_practice(stores)
    assert len(practice.polls) == 3
    stored = practice.polls["poll-2"].question
    assert poll["options"] == [{"text": option} for option in stored.options]
    assert poll["correct_option_ids"] == [stored.correct_option]
    [sent] = practice.deliveries.values()
    assert sent.sent_at is not None and sent.telegram_message_ids == [1, 2, 3, 4]
    # Every poll rates the lesson's core item, created on the spot if the lesson predates items.
    item = fake_items(stores).get_by_key(f"{lesson.topic_id}:core")
    assert item is not None and item.source_lesson_id == 1
    assert {p.item_id for p in practice.polls.values()} == {item.id}


def test_translation_is_sent_with_force_reply(stores: Stores, lesson: Lesson) -> None:
    practice = fake_practice(stores)
    quiz = practice.claim_delivery(1, "quiz", 3)
    assert quiz is not None
    practice.mark_sent(quiz.id, [2])
    practice.deliveries[quiz.id] = practice.deliveries[quiz.id].model_copy(update={"sent_at": at(10)})
    telegram = PollTelegram()

    assert deliver_due_practice(telegram.client(), settings(), stores, at(18, 0)) == "translation"

    [(method, payload)] = telegram.requests
    assert method == "sendMessage"
    assert lesson.translation[0].ru in payload["text"]
    assert payload["reply_markup"]["force_reply"] is True
