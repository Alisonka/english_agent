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
from english_agent.llm import AnthropicLLM, LLMUnavailable
from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import GradingInput
from english_agent.profile import current_reflection, refresh_profile
from english_agent.reflector import Reflector
from english_agent.storage.repositories import Stores
from english_agent.telegram.render import render_grade
from english_agent.telegram.sender import Sender, TelegramError

GRADING = "⏳ Проверяю…"
GRADE_FAILED = "Не получилось проверить перевод. Пришли ответ ещё раз чуть позже."
MAX_GRADING_ATTEMPTS = 3
DISPUTE_PREFIX = "dispute:"

Grader = Callable[[GradingInput, str], Awaitable[TranslationGrade]]

log = logging.getLogger(__name__)


def make_grader(settings: Settings, client: AsyncAnthropic | None = None) -> Grader:
    """Grade with GRADER_MODEL at GRADER_EFFORT through the process's Claude client."""
    llm = AnthropicLLM(settings, model=settings.grader_model, effort=settings.grader_effort, client=client)

    async def grade(task: GradingInput, text: str) -> TranslationGrade:
        return await grade_translation(llm, task, text)

    return grade


def dispute_keyboard(answer_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="Не согласна с оценкой", callback_data=f"{DISPUTE_PREFIX}{answer_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def refresh_profile_safely(stores: Stores, learner_id: int, reflect: Reflector | None = None) -> None:
    """The profile is derived data: a failure here is logged and never reaches the learner."""
    try:
        await refresh_profile(stores, learner_id, datetime.now(UTC), reflect=reflect)
    except SQLAlchemyError as error:
        log.warning("profile refresh failed for learner %s: %s", learner_id, error)


async def grade_answer(
    stores: Stores,
    sender: Sender,
    grade: Grader,
    answer_id: int,
    *,
    retry_transient: bool = False,
    reflect: Reflector | None = None,
) -> bool:
    """Grade a pending answer, store the result and show it; False when there was nothing to grade.

    The grader also sees the Reflector's current reading of the learner, for its explanations.
    With `retry_transient` (a queued job), a rate-limited or unreachable Claude API leaves the
    answer pending and re-raises, so the job is retried, until MAX_GRADING_ATTEMPTS are used.
    """
    now = datetime.now(UTC)
    task = await stores.practice.begin_grading(answer_id, now)
    if task is None:  # graded already (a redelivered job), or failed and waiting for a new answer
        return False
    chat_id, message_id = task.telegram_chat_id, task.answer.progress_message_id
    latest = await stores.profiles.latest(task.answer.learner_id)
    grading_input = task.input.model_copy(
        update={"learner": current_reflection(latest.profile if latest is not None else None, now)}
    )
    try:
        result = await grade(grading_input, task.answer.answer_text)
    except LLMUnavailable:
        if retry_transient and task.answer.grading_attempts < MAX_GRADING_ATTEMPTS:
            raise
        log.exception("grading failed: the model stayed unavailable")
        await stores.practice.fail_translation_answer(answer_id, "the model stayed unavailable")
        await _show(sender, chat_id, message_id, GRADE_FAILED)
        return True
    except Exception as error:  # the learner must hear back whatever went wrong
        log.exception("grading failed: %s: %s", type(error).__name__, error)
        await stores.practice.fail_translation_answer(answer_id, f"{type(error).__name__}: {error}")
        await _show(sender, chat_id, message_id, GRADE_FAILED)
        return True
    graded = await stores.practice.finish_translation_answer(answer_id, result)
    await _show(sender, chat_id, message_id, render_grade(result), reply_markup=dispute_keyboard(answer_id))
    await refresh_profile_safely(stores, graded.learner_id, reflect)
    return True


async def give_up_grading(stores: Stores, sender: Sender, answer_id: int, reason: str) -> None:
    """Every attempt failed or hung: release the task so the learner can answer again, and tell them."""
    task = await stores.practice.translation_for_grading(answer_id)
    if task is None or task.answer.status != "pending":
        return
    await stores.practice.fail_translation_answer(answer_id, reason)
    await _show(sender, task.telegram_chat_id, task.answer.progress_message_id, GRADE_FAILED)


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
