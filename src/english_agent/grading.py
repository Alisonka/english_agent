"""Grading one translation answer: the model's grade, stored, then shown in place of "⏳ Проверяю…".

`grade_answer` takes only the answer id and reads everything else from the database, so it can
run in the bot process or as a queued job, and running it twice is harmless: an answer that is no
longer pending is left alone. Whatever goes wrong, the learner hears back: a failed grading marks
the answer failed (they can send it again) and says so.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from anthropic import AsyncAnthropic
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import Settings
from english_agent.grader import grade_translation
from english_agent.llm import AnthropicLLM
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.profile import refresh_profile
from english_agent.storage.repositories import ProfileStore, Stores
from english_agent.telegram.render import render_grade
from english_agent.telegram.sender import Sender, TelegramError

GRADING = "⏳ Проверяю…"
GRADE_FAILED = "Не получилось проверить перевод. Пришли ответ ещё раз чуть позже."
DISPUTE_PREFIX = "dispute:"

Grader = Callable[[Lesson, str], Awaitable[TranslationGrade]]

log = logging.getLogger(__name__)


def make_grader(settings: Settings, client: AsyncAnthropic | None = None) -> Grader:
    """Grade with GRADER_MODEL at GRADER_EFFORT through the process's Claude client."""
    llm = AnthropicLLM(settings, model=settings.grader_model, effort=settings.grader_effort, client=client)

    async def grade(lesson: Lesson, text: str) -> TranslationGrade:
        return await grade_translation(llm, lesson, text)

    return grade


def dispute_keyboard(answer_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="Не согласна с оценкой", callback_data=f"{DISPUTE_PREFIX}{answer_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def refresh_profile_safely(profiles: ProfileStore, learner_id: int) -> None:
    """The profile is derived data: a failure here is logged and never reaches the learner."""
    try:
        await refresh_profile(profiles, learner_id, datetime.now(UTC))
    except SQLAlchemyError as error:
        log.warning("profile refresh failed for learner %s: %s", learner_id, error)


async def grade_answer(stores: Stores, sender: Sender, grade: Grader, answer_id: int) -> bool:
    """Grade a pending answer, store the result and show it; False when there was nothing to grade."""
    task = await stores.practice.translation_for_grading(answer_id)
    if task is None or task.answer.status != "pending":
        return False
    chat_id, message_id = task.telegram_chat_id, task.answer.progress_message_id
    try:
        result = await grade(task.lesson, task.answer.answer_text)
    except Exception as error:  # the learner must hear back whatever went wrong
        log.exception("grading failed: %s: %s", type(error).__name__, error)
        await stores.practice.fail_translation_answer(answer_id, f"{type(error).__name__}: {error}")
        await _show(sender, chat_id, message_id, GRADE_FAILED)
        return True
    graded = await stores.practice.finish_translation_answer(answer_id, result)
    await _show(sender, chat_id, message_id, render_grade(result), reply_markup=dispute_keyboard(answer_id))
    await refresh_profile_safely(stores.profiles, graded.learner_id)
    return True


async def _show(
    sender: Sender, chat_id: str, message_id: int | None, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Replace the progress message (or send a new one); a Telegram failure is only logged: the grade is stored."""
    try:
        if message_id is None:
            await sender.send_text(chat_id, text, reply_markup=reply_markup)
        else:
            await sender.edit_text(chat_id, message_id, text, reply_markup=reply_markup)
    except TelegramError as error:
        log.warning("could not deliver the grade: %s", error)
