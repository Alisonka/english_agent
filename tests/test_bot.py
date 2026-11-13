"""The bot's handlers, fed real `Update`s through the dispatcher, answering through a fake Bot API."""

from __future__ import annotations

import asyncio
from datetime import date, time
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from conftest import lesson_payload
from english_agent.bot.app import build_dispatcher
from english_agent.bot.deps import BotDeps, InProcessJobs
from english_agent.config import Settings
from english_agent.grading import Grader, grade_answer, refresh_profile_safely
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.storage import LearnerRecord
from english_agent.paths import available_cefr_levels
from english_agent.pipeline import resolve_cefr
from english_agent.storage import Stores
from english_agent.telegram.sender import Sender
from fakes import FakeLearners, FakePractice, FakeProfiles, fake_practice, fake_profiles, fake_stores
from samples import QUIZ
from telegram_fake import FakeTelegram, fake_bot, update

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


async def good_grade(lesson: Lesson, text: str) -> TranslationGrade:
    return TranslationGrade.model_validate(GRADE)


class InlineJobs:
    """Runs the handed-off work right away, so a test sees its effect when the update is handled."""

    def __init__(self, stores: Stores, sender: Sender, grade: Grader) -> None:
        self.stores, self.sender, self.grade = stores, sender, grade
        self.graded: list[int] = []

    async def grade_translation(self, answer_id: int) -> None:
        self.graded.append(answer_id)
        await grade_answer(self.stores, self.sender, self.grade, answer_id)

    async def refresh_profile(self, learner_id: int) -> None:
        await refresh_profile_safely(self.stores.profiles, learner_id)


class Harness:
    def __init__(self, stores: Stores | None = None, *, available: list[str] | None = None, grade: Grader = good_grade):
        self.stores = stores or fake_stores()[0]
        self.telegram = FakeTelegram()
        self.bot = fake_bot(self.telegram)
        sender = Sender(self.bot)
        self.jobs = InlineJobs(self.stores, sender, grade)
        settings = Settings(_env_file=None, telegram_bot_token="123:TOKEN", timezone="Europe/Moscow")
        deps = BotDeps(settings, self.stores, sender, self.jobs, available or ["A2", "B1"])
        self.dispatcher = build_dispatcher(deps)
        self._update_id = 0

    @property
    def learners(self) -> FakeLearners:
        assert isinstance(self.stores.learners, FakeLearners)
        return self.stores.learners

    async def feed(self, **content: Any) -> None:
        self._update_id += 1
        await self.dispatcher.feed_update(self.bot, update(self.bot, self._update_id, **content))

    async def text(self, text: str, chat_id: int = 42) -> None:
        await self.feed(message=message(text, chat_id))

    async def tap(self, data: str, *, message_id: int | None = 7) -> None:
        callback: dict[str, Any] = {"id": "cq", "from": user(), "chat_instance": "ci", "data": data}
        if message_id is not None:
            callback["message"] = {"message_id": message_id, "date": 0, "chat": {"id": 42, "type": "private"}}
        await self.feed(callback_query=callback)

    async def vote(self, poll_id: str, *options: int) -> None:
        answer = {"poll_id": poll_id, "user": user(), "option_ids": list(options), "option_persistent_ids": []}
        await self.feed(poll_answer=answer)


def user() -> dict[str, Any]:
    return {"id": 42, "is_bot": False, "first_name": "Alisa"}


def message(text: str, chat_id: int = 42) -> dict[str, Any]:
    return {"message_id": 1, "date": 0, "chat": {"id": chat_id, "type": "private"}, "from": user(), "text": text}


# --- level resolution -------------------------------------------------------


def learner(cefr: str) -> LearnerRecord:
    at = "2026-11-01T00:00:00Z"
    return LearnerRecord.model_validate(
        {"id": 1, "telegram_chat_id": "42", "cefr": cefr, "created_at": at, "updated_at": at}
    )


def test_resolve_cefr_prefers_explicit_then_saved_then_default() -> None:
    assert resolve_cefr("B2", learner("B1"), "A2") == "B2"
    assert resolve_cefr(None, learner("B1"), "A2") == "B1"
    assert resolve_cefr(None, None, "A2") == "A2"


def test_available_levels_match_curriculum_files() -> None:
    assert available_cefr_levels() == ["A2", "B1", "B2", "C1"]
    for level in ("A2", "B1", "B2", "C1"):
        assert load_curriculum(level).cefr == level


# --- commands and the level keyboard --------------------------------------------


async def test_level_command_offers_available_levels() -> None:
    bot = Harness()
    await bot.text("/level")
    [sent] = bot.telegram.of(SendMessage)
    assert [button.callback_data for button in sent.reply_markup.inline_keyboard[0]] == ["level:A2", "level:B1"]


async def test_start_creates_and_starts_the_learner_with_the_defaults() -> None:
    bot = Harness()
    await bot.text("/start")
    created = bot.learners.by_chat["42"]
    assert (created.cefr, created.timezone, created.curriculum_started_on is not None) == ("A2", "Europe/Moscow", True)
    assert bot.telegram.of(SendMessage)[0].reply_markup is not None


async def test_mylevel_reports_saved_level() -> None:
    bot = Harness()
    await bot.learners.set_cefr("42", "B1")
    await bot.text("/mylevel")
    assert "<b>B1</b>" in bot.telegram.texts[0]


async def test_tapping_a_level_saves_it_and_confirms() -> None:
    bot = Harness()
    await bot.tap("level:B1")
    assert bot.learners.by_chat["42"].cefr == "B1"
    assert [type(call) for call in bot.telegram.calls] == [AnswerCallbackQuery, EditMessageText]


async def test_a_level_without_a_curriculum_is_refused() -> None:
    bot = Harness()
    await bot.tap("level:A1")
    assert "42" not in bot.learners.by_chat
    [answer] = bot.telegram.calls
    assert isinstance(answer, AnswerCallbackQuery) and answer.text == "Этот уровень пока недоступен."


async def test_picking_a_level_restarts_a_finished_course() -> None:
    bot = Harness()
    bot.learners.started("42", date(2026, 11, 1))
    await bot.learners.deactivate("42", "course_finished")
    await bot.tap("level:B1", message_id=None)
    saved = bot.learners.by_chat["42"]
    assert (saved.cefr, saved.is_active) == ("B1", True)


async def test_unknown_text_and_commands_get_the_help() -> None:
    bot = Harness()
    await bot.text("hello")
    await bot.text("/unknown")
    assert all("/level" in text for text in bot.telegram.texts) and len(bot.telegram.texts) == 2


async def test_one_failing_update_does_not_stop_the_bot(caplog: pytest.LogCaptureFixture) -> None:
    bot = Harness()

    async def broken(chat_id: str) -> None:
        raise KeyError("unexpected shape")

    bot.learners.get = broken  # type: ignore[assignment,method-assign]
    await bot.text("/mylevel")  # raises inside the handler: logged and swallowed
    await bot.text("/level")
    assert "unexpected shape" in caplog.text
    assert len(bot.telegram.of(SendMessage)) == 1


# --- the learner's schedule ---------------------------------------------------------


async def test_time_sets_when_the_lesson_comes() -> None:
    bot = Harness()
    await bot.text("/time 8:30")
    assert bot.learners.by_chat["42"].lesson_send_time == time(8, 30)
    assert "Урок: 08:30" in bot.telegram.texts[0]


async def test_translation_sets_when_the_task_comes() -> None:
    bot = Harness()
    await bot.text("/translation 20.15")
    assert bot.learners.by_chat["42"].translation_send_time == time(20, 15)


async def test_time_rejects_a_bad_value_and_changes_nothing() -> None:
    bot = Harness()
    bot.learners.create("42", "A2")
    await bot.text("/time 25:00")
    assert "ЧЧ:ММ" in bot.telegram.texts[0]
    assert bot.learners.by_chat["42"].lesson_send_time == time(9, 0)


async def test_timezone_is_validated_and_saved() -> None:
    bot = Harness()
    await bot.text("/timezone Mars/Olympus")
    assert "часовой пояс" in bot.telegram.texts[0]
    await bot.text("/timezone Asia/Vladivostok")
    assert bot.learners.by_chat["42"].timezone == "Asia/Vladivostok"


async def test_pause_and_resume() -> None:
    bot = Harness()
    bot.learners.started("42", date(2026, 11, 1))
    await bot.text("/pause")
    assert not bot.learners.by_chat["42"].receives_lessons
    await bot.text("/resume")
    assert bot.learners.by_chat["42"].receives_lessons


async def test_status_shows_the_settings() -> None:
    bot = Harness()
    await bot.learners.set_cefr("42", "B1")
    await bot.text("/status")
    status = bot.telegram.texts[0]
    assert "<b>B1</b>" in status and "Europe/Moscow" in status and "18:00" in status


# --- practice: quiz answers and the translation task ------------------------------


async def practice_stores() -> tuple[Stores, FakePractice]:
    """A learner whose lesson has a sent quiz (polls p0..p2) and a sent translation task."""
    stores, learners, lessons = fake_stores()
    student = learners.create("42", "A2")
    lesson = Lesson.model_validate(lesson_payload())
    record = await lessons.record_sent(student.id, date(2026, 11, 6), lesson, [1], curriculum_version=1)
    practice = fake_practice(stores)
    quiz = await practice.claim_delivery(record.id, "quiz", 3)
    assert quiz is not None
    for position, item in enumerate(QUIZ):
        await practice.add_poll(
            quiz.id,
            position=position,
            telegram_poll_id=f"p{position}",
            telegram_message_id=10 + position,
            item_id=1,
            question=QuizQuestion.model_validate(item),
        )
    await practice.mark_sent(quiz.id, [10, 11, 12])
    task = await practice.claim_delivery(record.id, "translation", 3)
    assert task is not None
    await practice.mark_sent(task.id, [20])
    return stores, practice


async def test_quiz_summary_comes_after_the_last_answer_and_refreshes_the_profile() -> None:
    stores, practice = await practice_stores()
    bot = Harness(stores)

    await bot.vote("p0", 1)
    await bot.vote("p1", 0)
    assert bot.telegram.calls == []  # no message until every poll is answered
    assert fake_profiles(stores).versions == {}

    await bot.vote("p2", 1)
    await bot.vote("p2", 0)  # a repeat is ignored
    [summary] = bot.telegram.texts
    assert "Итог: 2/3" in summary
    assert ("quiz", "It is milk in the fridge.", "There is milk in the fridge.") in practice.errors
    [record] = fake_profiles(stores).versions[1]
    assert record.profile.cefr == "A2"


async def test_retracted_vote_is_ignored() -> None:
    stores, practice = await practice_stores()
    await Harness(stores).vote("p0")
    assert practice.poll_answers == {}


async def test_text_answers_the_open_translation_task() -> None:
    stores, practice = await practice_stores()
    bot = Harness(stores)

    await bot.text("There is a window in my room.")

    progress, result = bot.telegram.calls
    assert isinstance(progress, SendMessage) and progress.text == "⏳ Проверяю…"
    assert isinstance(result, EditMessageText) and "Результат: 1/3" in result.text
    assert result.reply_markup is not None and result.reply_markup.inline_keyboard[0][0].callback_data == "dispute:1"
    [answer] = practice.translation_answers.values()
    assert answer.status == "graded"
    assert ("text", "There is no children in the park.", "There are no children in the park.") in practice.errors
    assert isinstance(stores.profiles, FakeProfiles) and [r.version for r in stores.profiles.versions[1]] == [1]

    await bot.text("ещё ответ")
    assert "/level" in bot.telegram.texts[-1]  # task closed: back to the help text


async def test_failed_grading_lets_the_learner_retry() -> None:
    async def broken(lesson: Lesson, text: str) -> TranslationGrade:
        raise RuntimeError("model is down")

    stores, practice = await practice_stores()
    await Harness(stores, grade=broken).text("There is a window.")
    [answer] = practice.translation_answers.values()
    assert answer.status == "failed"

    retry = Harness(stores)
    await retry.text("There is a window in my room.")
    [answer] = practice.translation_answers.values()
    assert answer.status == "graded"


async def test_grading_a_graded_answer_again_changes_nothing() -> None:
    stores, practice = await practice_stores()
    bot = Harness(stores)
    await bot.text("There is a window in my room.")
    calls = len(bot.telegram.calls)

    assert await grade_answer(stores, Sender(bot.bot), good_grade, 1) is False  # e.g. a redelivered job
    assert len(bot.telegram.calls) == calls


async def test_dispute_flags_the_grade() -> None:
    stores, practice = await practice_stores()
    bot = Harness(stores)
    await bot.text("There is a window in my room.")

    await bot.tap("dispute:1", message_id=None)

    assert practice.translation_answers[1].disputed is True
    answer = bot.telegram.calls[-1]
    assert isinstance(answer, AnswerCallbackQuery) and answer.text == "Спасибо, оценка отмечена как спорная."


# --- background jobs of the bot process -------------------------------------------


async def test_in_process_jobs_run_in_the_background_and_drain() -> None:
    stores, practice = await practice_stores()
    task = await practice.open_translation("42")
    assert task is not None
    answer = await practice.start_translation_answer(task.delivery.id, task.learner_id, "There is a window.")
    assert answer is not None
    telegram = FakeTelegram()
    jobs = InProcessJobs(stores, Sender(fake_bot(telegram)), good_grade)

    await jobs.grade_translation(answer.id)
    assert jobs.pending == 1  # handed off: the handler returns at once
    await jobs.drain(timeout=5)

    assert practice.translation_answers[answer.id].status == "graded"
    assert jobs.pending == 0


async def test_draining_cancels_jobs_that_overrun() -> None:
    stores, _ = await practice_stores()
    started: list[bool] = []

    async def slow(lesson: Lesson, text: str) -> TranslationGrade:
        started.append(True)
        await asyncio.sleep(60)
        raise AssertionError("never finishes")

    practice = fake_practice(stores)
    task = await practice.open_translation("42")
    assert task is not None
    answer = await practice.start_translation_answer(task.delivery.id, task.learner_id, "x")
    assert answer is not None
    jobs = InProcessJobs(stores, Sender(fake_bot(FakeTelegram())), slow)
    await jobs.grade_translation(answer.id)
    await asyncio.sleep(0)
    await jobs.drain(timeout=0.01)

    assert started and jobs.pending == 0
    assert practice.translation_answers[answer.id].status == "pending"  # left for a later retry
