"""Update handlers: commands, the level keyboard, the learner's schedule, quiz answers, translations.

Ported from the hand-written polling loop; the texts and buttons are unchanged (`level:<CEFR>`
and `dispute:<answer id>` callback data keep working for messages sent before the move).
Every handler finishes with a few short database transactions and Telegram calls; grading is
handed to `BotDeps.jobs`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import cast

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PollAnswer,
)

from english_agent.bot.deps import BotDeps
from english_agent.grading import DISPUTE_PREFIX, GRADING
from english_agent.models.cefr import Cefr
from english_agent.models.schedule import LearnerSchedule, local_today, parse_clock, valid_timezone
from english_agent.models.storage import LearnerRecord
from english_agent.telegram.render import render_quiz_summary
from english_agent.telegram.sender import TelegramError

log = logging.getLogger(__name__)

PROMPT = "Выбери свой уровень английского. Поменять можно в любой момент командой /level."
HELP = (
    "Я присылаю ежедневные уроки по твоему уровню.\n"
    "• /level — выбрать или сменить уровень\n"
    "• /mylevel — какой уровень сейчас стоит\n"
    "• /time 09:00 — во сколько присылать урок\n"
    "• /translation 18:00 — во сколько присылать задание на перевод\n"
    "• /timezone Europe/Moscow — твой часовой пояс\n"
    "• /pause и /resume — приостановить и продолжить уроки\n"
    "• /status — текущие настройки"
)
BAD_TIME = "Напиши время в формате ЧЧ:ММ, например: /{command} 09:30"
BAD_TIMEZONE = "Не знаю такой часовой пояс. Пример: /timezone Europe/Moscow или /timezone Asia/Vladivostok"
PAUSED = "Уроки на паузе. Продолжить — /resume."
RESUMED = "Уроки продолжаются с той темы, на которой остановились."
DISPUTED = "Спасибо, оценка отмечена как спорная."
UNAVAILABLE_LEVEL = "Этот уровень пока недоступен."
LEVEL_PREFIX = "level:"

# Shown in Telegram's `/` command menu (registered with setMyCommands on startup).
COMMANDS: list[tuple[str, str]] = [
    ("level", "Выбрать или сменить уровень"),
    ("mylevel", "Показать текущий уровень"),
    ("time", "Во сколько присылать урок"),
    ("translation", "Во сколько присылать задание на перевод"),
    ("timezone", "Часовой пояс"),
    ("pause", "Приостановить уроки"),
    ("resume", "Продолжить уроки"),
    ("status", "Текущие настройки"),
]


def level_keyboard(available: list[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=level, callback_data=f"{LEVEL_PREFIX}{level}") for level in available]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def render_status(learner: LearnerRecord) -> str:
    state = "на паузе" if learner.paused_at is not None else "идут" if learner.is_active else "остановлены (/start)"
    return (
        f"Уровень: <b>{learner.cefr}</b>\n"
        f"Часовой пояс: {learner.timezone}\n"
        f"Урок: {learner.lesson_send_time:%H:%M}, перевод: {learner.translation_send_time:%H:%M}\n"
        f"Уроки: {state}"
    )


async def _learner(deps: BotDeps, chat_id: str) -> LearnerRecord:
    return await deps.stores.learners.get_or_create(
        chat_id, deps.settings.learner_cefr, deps.settings.default_schedule()
    )


# -- commands ------------------------------------------------------------------------


async def start(message: Message, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    learner = await _learner(deps, chat_id)
    await deps.stores.learners.start_course(chat_id, local_today(learner.timezone, datetime.now(UTC)))
    await deps.sender.send_text(chat_id, PROMPT, reply_markup=level_keyboard(deps.available_levels))


async def level(message: Message, deps: BotDeps) -> None:
    await deps.sender.send_text(str(message.chat.id), PROMPT, reply_markup=level_keyboard(deps.available_levels))


async def my_level(message: Message, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    learner = await deps.stores.learners.get(chat_id)
    current = learner.cefr if learner is not None else deps.settings.learner_cefr
    await deps.sender.send_text(chat_id, f"Текущий уровень: <b>{current}</b>. Сменить — /level.")


async def _change_schedule(deps: BotDeps, chat_id: str, **changes: object) -> LearnerRecord:
    learner = await _learner(deps, chat_id)
    schedule = LearnerSchedule.model_validate({**learner.schedule.model_dump(), **changes})
    updated = await deps.stores.learners.set_schedule(chat_id, schedule)
    assert updated is not None
    return updated


async def set_time(message: Message, command: CommandObject, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    clock = parse_clock(command.args or "")
    if clock is None:
        await deps.sender.send_text(chat_id, BAD_TIME.format(command=command.command))
        return
    field = "lesson_send_time" if command.command == "time" else "translation_send_time"
    learner = await _change_schedule(deps, chat_id, **{field: clock})
    await deps.sender.send_text(chat_id, render_status(learner))


async def set_timezone(message: Message, command: CommandObject, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    try:
        zone = valid_timezone((command.args or "").strip())
    except ValueError:
        await deps.sender.send_text(chat_id, BAD_TIMEZONE)
        return
    learner = await _change_schedule(deps, chat_id, timezone=zone)
    await deps.sender.send_text(chat_id, render_status(learner))


async def pause(message: Message, command: CommandObject, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    await _learner(deps, chat_id)
    paused = command.command == "pause"
    await deps.stores.learners.set_paused(chat_id, paused)
    await deps.sender.send_text(chat_id, PAUSED if paused else RESUMED)


async def status(message: Message, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    await deps.sender.send_text(chat_id, render_status(await _learner(deps, chat_id)))


# -- the translation answer and everything else ------------------------------------------------


async def text(message: Message, deps: BotDeps) -> None:
    chat_id = str(message.chat.id)
    if not await _take_translation_answer(deps, chat_id, (message.text or "").strip()):
        await deps.sender.send_text(chat_id, HELP)


async def anything_else(message: Message, deps: BotDeps) -> None:
    await deps.sender.send_text(str(message.chat.id), HELP)


async def _take_translation_answer(deps: BotDeps, chat_id: str, answer_text: str) -> bool:
    """Treat the text as the answer to the open translation task; False when there is none."""
    practice = deps.stores.practice
    task = await practice.open_translation(chat_id)
    if task is None:
        return False
    answer = await practice.start_translation_answer(task.delivery.id, task.learner_id, answer_text)
    if answer is None:
        return False
    try:
        message_id = await deps.sender.send_text(chat_id, GRADING)
    except TelegramError:
        # Without this the answer would stay pending and the learner could never send it again.
        await practice.fail_translation_answer(answer.id, "could not send the progress message")
        raise
    await practice.set_progress_message(answer.id, message_id)
    await deps.jobs.grade_translation(answer.id)
    return True


# -- buttons and polls --------------------------------------------------------------------


async def pick_level(callback: CallbackQuery, deps: BotDeps) -> None:
    chosen = (callback.data or "").removeprefix(LEVEL_PREFIX)
    if chosen not in deps.available_levels:
        await callback.answer(UNAVAILABLE_LEVEL)
        return
    # In a private chat the chat id equals the user id, so `from` covers a callback without a message.
    message = callback.message
    chat_id = str(message.chat.id if message is not None else callback.from_user.id)
    learner = await deps.stores.learners.set_cefr(chat_id, cast(Cefr, chosen))
    # Picking a level also starts the course, or restarts it after the previous level was finished.
    await deps.stores.learners.start_course(chat_id, local_today(learner.timezone, datetime.now(UTC)))
    await callback.answer(f"Уровень обновлён: {chosen}")
    if message is not None and not isinstance(message, InaccessibleMessage):
        await deps.sender.edit_text(
            chat_id, message.message_id, f"✅ Твой уровень: <b>{chosen}</b>.\nПоменять в любой момент — /level."
        )


async def dispute(callback: CallbackQuery, deps: BotDeps) -> None:
    answer_id = (callback.data or "").removeprefix(DISPUTE_PREFIX)
    disputed = answer_id.isdigit() and await deps.stores.practice.dispute(int(answer_id))
    await callback.answer(DISPUTED if disputed else None)


async def other_button(callback: CallbackQuery) -> None:
    await callback.answer()


async def poll_answer(answer: PollAnswer, deps: BotDeps) -> None:
    if not answer.option_ids or answer.user is None:
        return  # a retracted vote, or an answer on behalf of a chat
    chat_id = str(answer.user.id)  # in a private chat the chat id equals the user id
    result = await deps.stores.practice.record_poll_answer(answer.poll_id, chat_id, list(answer.option_ids))
    if result is not None and result.completed:
        await deps.sender.send_text(chat_id, render_quiz_summary(result.results))
        await deps.jobs.refresh_profile(result.learner_id)


async def on_error(event: ErrorEvent) -> bool:
    """Log and swallow: one bad update must never stop the bot (the old loop crashed on unexpected shapes)."""
    log.error("update %s failed: %r", event.update.update_id, event.exception, exc_info=event.exception)
    return True


def build_router() -> Router:
    """A new router with every handler (a router can belong to one dispatcher only)."""
    router = Router(name="english_agent")
    router.message.register(start, CommandStart())
    router.message.register(level, Command("level"))
    router.message.register(my_level, Command("mylevel"))
    router.message.register(set_time, Command("time", "translation"))
    router.message.register(set_timezone, Command("timezone"))
    router.message.register(pause, Command("pause", "resume"))
    router.message.register(status, Command("status"))
    router.message.register(text, F.text & ~F.text.startswith("/"))
    router.message.register(anything_else)
    router.callback_query.register(pick_level, F.data.startswith(LEVEL_PREFIX))
    router.callback_query.register(dispute, F.data.startswith(DISPUTE_PREFIX))
    router.callback_query.register(other_button)
    router.poll_answer.register(poll_answer)
    router.errors.register(on_error)
    return router
