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
from english_agent.models.storage import LearnerRecord, LessonRecord, PracticeDeliveryRecord
from english_agent.practice import deliver_due_practice, due_practice, shuffle_options
from english_agent.storage import Stores
from english_agent.telegram import TelegramClient
from fakes import fake_items, fake_practice, fake_stores

MOSCOW = ZoneInfo("Europe/Moscow")
DAY = date(2026, 11, 6)
LESSON_AT = datetime(2026, 11, 6, 9, 0, tzinfo=MOSCOW)


def settings() -> Settings:
    return Settings(_env_file=None, telegram_chat_id="42", timezone="Europe/Moscow")


def learner(**changes: Any) -> LearnerRecord:
    """A Moscow learner on the default schedule: quiz an hour after the lesson, translation at 18:00."""
    return LearnerRecord.model_validate(
        {
            "id": 1,
            "telegram_chat_id": "42",
            "cefr": "A2",
            "created_at": LESSON_AT,
            "updated_at": LESSON_AT,
            "curriculum_started_on": DAY,
            **changes,
        }
    )


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
    assert due_practice(learner(), settings(), record, None, None, at(9, 59)) is None
    assert due_practice(learner(), settings(), record, None, None, at(10, 0)) == "quiz"


def test_translation_waits_for_evening_and_for_the_quiz(lesson: Lesson) -> None:
    record = sent_lesson(lesson)
    quiz = delivery("quiz", at(10))
    assert due_practice(learner(), settings(), record, quiz, None, at(17, 55)) is None
    assert due_practice(learner(), settings(), record, quiz, None, at(18, 0)) == "translation"
    assert due_practice(learner(), settings(), record, quiz, delivery("translation", at(18)), at(18, 5)) is None


def test_late_lesson_shifts_the_whole_chain(lesson: Lesson) -> None:
    record = sent_lesson(lesson, sent_at=at(17, 30))
    assert due_practice(learner(), settings(), record, None, None, at(18, 0)) is None
    assert due_practice(learner(), settings(), record, None, None, at(18, 30)) == "quiz"
    quiz = delivery("quiz", at(18, 30))
    assert due_practice(learner(), settings(), record, quiz, None, at(19, 0)) is None
    assert due_practice(learner(), settings(), record, quiz, None, at(19, 30)) == "translation"


def test_nothing_is_sent_after_the_cutoff(lesson: Lesson) -> None:
    record = sent_lesson(lesson, sent_at=at(21, 0))
    assert due_practice(learner(), settings(), record, None, None, at(22, 0)) is None


def test_learners_own_schedule_decides(lesson: Lesson) -> None:
    record = sent_lesson(lesson)
    vladivostok = learner(timezone="Asia/Vladivostok", quiz_delay_minutes=30, translation_send_time=time(20, 0))
    # 09:30 Moscow is 16:30 in Vladivostok: the quiz is due after 30 minutes, the translation at 20:00 there.
    assert due_practice(vladivostok, settings(), record, None, None, at(9, 30)) == "quiz"
    quiz = delivery("quiz", at(9, 30))
    assert due_practice(vladivostok, settings(), record, quiz, None, at(12, 30)) is None  # 19:30 there
    assert due_practice(vladivostok, settings(), record, quiz, None, at(13, 0)) == "translation"


def test_lesson_without_practice_gets_none(lesson: Lesson) -> None:
    record = sent_lesson(lesson.model_copy(update={"quiz": [], "translation": []}))
    assert due_practice(learner(), settings(), record, None, None, at(12)) is None


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
    student = learners.started("42", DAY)
    record = lessons.record_sent(student.id, DAY, lesson, [1], curriculum_version=1)
    lessons.rows[(student.id, DAY)] = record.model_copy(update={"sent_at": LESSON_AT})
    return stores


def test_quiz_is_sent_once_and_recorded(stores: Stores, lesson: Lesson) -> None:
    telegram = PollTelegram()
    now = at(11)

    assert deliver_due_practice(telegram.client(), settings(), stores, now, rng=random.Random(1)) == [("42", "quiz")]
    assert deliver_due_practice(telegram.client(), settings(), stores, now) == []  # already claimed

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

    assert deliver_due_practice(telegram.client(), settings(), stores, at(18, 0)) == [("42", "translation")]

    [(method, payload)] = telegram.requests
    assert method == "sendMessage"
    assert lesson.translation[0].ru in payload["text"]
    assert payload["reply_markup"]["force_reply"] is True


def test_paused_learner_gets_no_practice(stores: Stores) -> None:
    stores.learners.set_paused("42", True)
    telegram = PollTelegram()
    assert deliver_due_practice(telegram.client(), settings(), stores, at(11)) == []
    assert telegram.requests == []


def test_blocked_chat_is_deactivated_and_others_still_get_their_quiz(lesson: Lesson) -> None:
    stores, learners, lessons = fake_stores()
    for chat_id in ("42", "43"):
        student = learners.started(chat_id, DAY)
        record = lessons.record_sent(student.id, DAY, lesson, [1], curriculum_version=1)
        lessons.rows[(student.id, DAY)] = record.model_copy(update={"sent_at": LESSON_AT})

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content or b"{}").get("chat_id") == "42":
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})
        result: dict[str, Any] = {"message_id": 1, "poll": {"id": f"poll-{len(fake_practice(stores).polls)}"}}
        return httpx.Response(200, json={"ok": True, "result": result})

    client = TelegramClient(httpx.Client(transport=httpx.MockTransport(handler)), "1:T")
    assert deliver_due_practice(client, settings(), stores, at(11)) == [("43", "quiz")]
    blocked = learners.get("42")
    assert blocked is not None and (blocked.is_active, blocked.deactivated_reason) == (False, "blocked")
