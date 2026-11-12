from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

from conftest import lesson_payload
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import LearnerRecord
from english_agent.paths import available_cefr_levels
from english_agent.pipeline import resolve_cefr
from english_agent.telegram import TelegramClient
from english_agent.telegram.leveling import Grader, PracticeHandlers, process_updates
from fakes import FakeLearners, FakePractice, FakeProfiles, fake_practice, fake_profiles, fake_stores
from samples import QUIZ

TOKEN = "123:TOKEN"


class RecordingTelegram:
    """A TelegramClient whose calls are recorded and always answered ok."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def client(self) -> TelegramClient:
        return TelegramClient(httpx.Client(transport=httpx.MockTransport(self._handler)), TOKEN)


def learner(cefr: str) -> LearnerRecord:
    now = datetime.now(UTC)
    return LearnerRecord.model_validate(
        {"id": 1, "telegram_chat_id": "42", "cefr": cefr, "created_at": now, "updated_at": now}
    )


# --- level resolution -------------------------------------------------------


def test_resolve_cefr_prefers_explicit() -> None:
    assert resolve_cefr("B2", learner("B1"), "A2") == "B2"


def test_resolve_cefr_uses_saved_choice() -> None:
    assert resolve_cefr(None, learner("B1"), "A2") == "B1"


def test_resolve_cefr_falls_back_to_default() -> None:
    assert resolve_cefr(None, None, "A2") == "A2"


# --- interactive handler ----------------------------------------------------


def _update(update_id: int, **content: Any) -> dict[str, Any]:
    return {"update_id": update_id, **content}


def test_level_command_offers_available_levels() -> None:
    fake = RecordingTelegram()
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/level"})]

    offset = process_updates(fake.client(), updates, learners=FakeLearners(), available=["A2", "B1"], default_cefr="A2")

    assert offset == 2
    method, payload = fake.requests[0]
    assert method == "sendMessage"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["level:A2", "level:B1"]


def test_start_creates_learner_with_default_level() -> None:
    learners = FakeLearners()
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/start"})]

    process_updates(RecordingTelegram().client(), updates, learners=learners, available=["A2"], default_cefr="A2")

    created = learners.get("42")
    assert created is not None
    assert created.cefr == "A2"
    assert created.curriculum_started_on is not None  # /start starts the course


def test_mylevel_reports_saved_level() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    learners.set_cefr("42", "B1")
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/mylevel"})]

    process_updates(fake.client(), updates, learners=learners, available=["A2", "B1"], default_cefr="A2")

    assert "<b>B1</b>" in fake.requests[0][1]["text"]


def test_callback_saves_level_and_confirms() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    callback = {"id": "cq1", "data": "level:B1", "message": {"chat": {"id": 42}, "message_id": 7}}
    updates = [_update(5, callback_query=callback)]

    offset = process_updates(fake.client(), updates, learners=learners, available=["A2", "B1"], default_cefr="A2")

    assert offset == 6
    saved = learners.get("42")
    assert saved is not None
    assert saved.cefr == "B1"
    methods = [method for method, _ in fake.requests]
    assert methods == ["answerCallbackQuery", "editMessageText"]


def test_callback_rejects_unavailable_level() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    callback = {"id": "cq1", "data": "level:A1", "message": {"chat": {"id": 42}, "message_id": 7}}

    process_updates(
        fake.client(),
        [_update(1, callback_query=callback)],
        learners=learners,
        available=["A2", "B1"],
        default_cefr="A2",
    )

    assert learners.get("42") is None
    assert [method for method, _ in fake.requests] == ["answerCallbackQuery"]


def test_empty_batch_keeps_offset_none() -> None:
    fake = RecordingTelegram()
    assert process_updates(fake.client(), [], learners=FakeLearners(), available=["A2"], default_cefr="A2") is None
    assert fake.requests == []


# --- curriculum selection ---------------------------------------------------


def test_available_levels_match_curriculum_files() -> None:
    assert available_cefr_levels() == ["A2", "B1", "B2", "C1"]


def test_load_curriculum_by_level() -> None:
    for level in ("A2", "B1", "B2", "C1"):
        assert load_curriculum(level).cefr == level


# --- practice: quiz answers and the translation task ------------------------------


def _practice_setup(grade: Grader | None = None) -> tuple[RecordingTelegram, FakePractice, PracticeHandlers]:
    stores, learners, lessons = fake_stores()
    learner_record = learners.get_or_create("42", "A2")
    lesson = Lesson.model_validate(lesson_payload())
    record = lessons.record_sent(learner_record.id, date(2026, 11, 6), lesson, [1], curriculum_version=1)
    practice = fake_practice(stores)
    quiz = practice.claim_delivery(record.id, "quiz", 3)
    assert quiz is not None
    for position, item in enumerate(QUIZ):
        practice.add_poll(
            quiz.id,
            position=position,
            telegram_poll_id=f"p{position}",
            telegram_message_id=10 + position,
            item_id=1,
            question=QuizQuestion.model_validate(item),
        )
    practice.mark_sent(quiz.id, [10, 11, 12])
    task = practice.claim_delivery(record.id, "translation", 3)
    assert task is not None
    practice.mark_sent(task.id, [20])
    handlers = PracticeHandlers(
        practice,
        grade or (lambda lesson, text: TranslationGrade.model_validate(GRADE)),
        profiles=fake_profiles(stores),
    )
    return RecordingTelegram(), practice, handlers


GRADE: dict[str, Any] = {
    "sentences": [
        {
            "n": 1,
            "learner_text": "There is a window in my room.",
            "verdict": "correct",
            "corrected": "There is a window in my room.",
            "explanation_ru": "",
        },
        {
            "n": 2,
            "learner_text": "There is no children in the park.",
            "verdict": "fixed",
            "corrected": "There are no children in the park.",
            "explanation_ru": "Children — множественное число, поэтому there are.",
            "error_type": "target_form",
        },
        {"n": 3, "learner_text": "", "verdict": "missing", "corrected": "Is there a cafe here?", "explanation_ru": ""},
    ],
    "focus_ru": "Смотри на число существительного после there.",
}


def _poll_answer(update_id: int, poll_id: str, option: int) -> dict[str, Any]:
    return _update(update_id, poll_answer={"poll_id": poll_id, "user": {"id": 42}, "option_ids": [option]})


def _run(fake: RecordingTelegram, updates: list[dict[str, Any]], practice: PracticeHandlers) -> None:
    process_updates(
        fake.client(), updates, learners=FakeLearners(), available=["A2"], default_cefr="A2", practice=practice
    )


def test_quiz_summary_comes_after_the_last_answer() -> None:
    fake, store, practice = _practice_setup()

    _run(fake, [_poll_answer(1, "p0", 1), _poll_answer(2, "p1", 0)], practice)
    assert fake.requests == []  # no message until every poll is answered

    _run(fake, [_poll_answer(3, "p2", 1), _poll_answer(4, "p2", 0)], practice)  # a repeat is ignored

    [(method, payload)] = fake.requests
    assert method == "sendMessage"
    assert "Итог: 2/3" in payload["text"]
    assert ("quiz", "It is milk in the fridge.", "There is milk in the fridge.") in store.errors


def test_profile_is_refreshed_once_the_quiz_is_complete() -> None:
    fake, _, practice = _practice_setup()
    assert isinstance(practice.profiles, FakeProfiles)

    _run(fake, [_poll_answer(1, "p0", 1), _poll_answer(2, "p1", 0)], practice)
    assert practice.profiles.versions == {}

    _run(fake, [_poll_answer(3, "p2", 1)], practice)
    [record] = practice.profiles.versions[1]
    assert record.version == 1 and record.profile.cefr == "A2"


def test_profile_is_refreshed_after_a_graded_translation() -> None:
    fake, _, practice = _practice_setup()
    assert isinstance(practice.profiles, FakeProfiles)

    _run(fake, [_update(1, message={"chat": {"id": 42}, "text": "There is a window in my room."})], practice)

    assert [record.version for record in practice.profiles.versions[1]] == [1]


def test_retracted_vote_is_ignored() -> None:
    fake, store, practice = _practice_setup()
    _run(fake, [_update(1, poll_answer={"poll_id": "p0", "user": {"id": 42}, "option_ids": []})], practice)
    assert store.poll_answers == {}


def test_text_answers_the_open_translation_task() -> None:
    fake, store, practice = _practice_setup()

    _run(fake, [_update(1, message={"chat": {"id": 42}, "text": "There is a window in my room."})], practice)

    methods = [method for method, _ in fake.requests]
    assert methods == ["sendMessage", "editMessageText"]
    assert fake.requests[0][1]["text"] == "⏳ Проверяю…"
    result = fake.requests[1][1]
    assert "Результат: 1/3" in result["text"]
    assert result["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "dispute:1"
    [answer] = store.translation_answers.values()
    assert answer.status == "graded"
    assert ("text", "There is no children in the park.", "There are no children in the park.") in store.errors

    _run(fake, [_update(2, message={"chat": {"id": 42}, "text": "ещё ответ"})], practice)
    assert "/level" in fake.requests[-1][1]["text"]  # task closed: back to the help text


def test_failed_grading_lets_the_learner_retry() -> None:
    def broken(lesson: Lesson, text: str) -> TranslationGrade:
        raise RuntimeError("model is down")

    fake, store, practice = _practice_setup(grade=broken)
    _run(fake, [_update(1, message={"chat": {"id": 42}, "text": "There is a window."})], practice)

    assert "Не получилось проверить" in fake.requests[-1][1]["text"]
    [answer] = store.translation_answers.values()
    assert answer.status == "failed"

    retry = PracticeHandlers(store, lambda lesson, text: TranslationGrade.model_validate(GRADE))
    _run(fake, [_update(2, message={"chat": {"id": 42}, "text": "There is a window in my room."})], retry)
    [answer] = store.translation_answers.values()
    assert answer.status == "graded"


def test_dispute_flags_the_grade() -> None:
    fake, store, practice = _practice_setup()
    _run(fake, [_update(1, message={"chat": {"id": 42}, "text": "There is a window in my room."})], practice)

    _run(fake, [_update(2, callback_query={"id": "cq", "data": "dispute:1", "from": {"id": 42}})], practice)

    assert store.translation_answers[1].disputed is True
    method, payload = fake.requests[-1]
    assert method == "answerCallbackQuery"
    assert payload["text"] == "Спасибо, оценка отмечена как спорная."


# --- the learner's schedule ---------------------------------------------------


def _command(learners: FakeLearners, text: str) -> list[tuple[str, dict[str, Any]]]:
    fake = RecordingTelegram()
    updates = [_update(1, message={"chat": {"id": 42}, "text": text})]
    schedule = LearnerSchedule(timezone="Europe/Moscow")
    process_updates(
        fake.client(), updates, learners=learners, available=["A2"], default_cefr="A2", default_schedule=schedule
    )
    return fake.requests


def test_time_sets_when_the_lesson_comes() -> None:
    learners = FakeLearners()
    [(_, reply)] = _command(learners, "/time 8:30")
    saved = learners.get("42")
    assert saved is not None and saved.lesson_send_time == time(8, 30)
    assert "Урок: 08:30" in reply["text"]


def test_translation_sets_when_the_task_comes() -> None:
    learners = FakeLearners()
    _command(learners, "/translation 20.15")
    saved = learners.get("42")
    assert saved is not None and saved.translation_send_time == time(20, 15)


def test_time_rejects_a_bad_value_and_changes_nothing() -> None:
    learners = FakeLearners()
    learners.get_or_create("42", "A2")
    [(_, reply)] = _command(learners, "/time 25:00")
    assert "ЧЧ:ММ" in reply["text"]
    saved = learners.get("42")
    assert saved is not None and saved.lesson_send_time == time(9, 0)


def test_timezone_is_validated_and_saved() -> None:
    learners = FakeLearners()
    [(_, bad)] = _command(learners, "/timezone Mars/Olympus")
    assert "часовой пояс" in bad["text"]
    _command(learners, "/timezone Asia/Vladivostok")
    saved = learners.get("42")
    assert saved is not None and saved.timezone == "Asia/Vladivostok"


def test_pause_and_resume() -> None:
    learners = FakeLearners()
    learners.started("42", date(2026, 11, 1))
    _command(learners, "/pause")
    paused = learners.get("42")
    assert paused is not None and paused.paused_at is not None and not paused.receives_lessons
    _command(learners, "/resume")
    resumed = learners.get("42")
    assert resumed is not None and resumed.paused_at is None and resumed.receives_lessons


def test_status_shows_the_settings() -> None:
    learners = FakeLearners()
    learners.set_cefr("42", "B1")
    [(_, reply)] = _command(learners, "/status")
    assert "<b>B1</b>" in reply["text"] and "Europe/Moscow" in reply["text"] and "18:00" in reply["text"]


def test_picking_a_level_restarts_a_finished_course() -> None:
    learners = FakeLearners()
    learners.started("42", date(2026, 11, 1))
    learners.deactivate("42", "course_finished")
    callback = {"id": "cq1", "data": "level:B1", "message": {"chat": {"id": 42}, "message_id": 7}}

    process_updates(
        RecordingTelegram().client(),
        [_update(1, callback_query=callback)],
        learners=learners,
        available=["A2", "B1"],
        default_cefr="A2",
    )
    saved = learners.get("42")
    assert saved is not None and (saved.cefr, saved.is_active) == ("B1", True)
