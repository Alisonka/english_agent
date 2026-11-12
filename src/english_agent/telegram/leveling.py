"""The bot's interactive side: the CEFR level, the learner's schedule, quiz answers and the translation task.

    uv run python -m english_agent.telegram level

A small long-polling loop over the sync `TelegramClient` (no aiogram yet). It answers
/level and /start with an inline keyboard of the levels that actually have a curriculum
and stores the choice in the `learners` table the daily pipeline reads; /time, /translation,
/timezone, /pause, /resume and /status manage the learner's own schedule; records answers to
the quiz polls (and sends the result after the last one); and takes a plain-text message
as the answer to an open translation task, graded in a background thread so polling never
waits for the model. On the server the same loop runs inside `english_agent.app`, which
must stay the only `getUpdates` consumer of the bot.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import Settings
from english_agent.grader import grade_translation
from english_agent.llm.anthropic_client import AnthropicLLM
from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.models.schedule import LearnerSchedule, local_today, parse_clock, valid_timezone
from english_agent.models.storage import LearnerRecord
from english_agent.paths import available_cefr_levels
from english_agent.profile import refresh_profile
from english_agent.storage.repositories import LearnerStore, PracticeStore, ProfileStore, Stores, open_stores
from english_agent.telegram.client import TelegramClient, TelegramError
from english_agent.telegram.render import render_grade, render_quiz_summary

_LONG_POLL_SECONDS = 25
_RETRY_SECONDS = 5
_ALLOWED_UPDATES = ["message", "callback_query", "poll_answer"]

_PROMPT = "Выбери свой уровень английского. Поменять можно в любой момент командой /level."
_HELP = (
    "Я присылаю ежедневные уроки по твоему уровню.\n"
    "• /level — выбрать или сменить уровень\n"
    "• /mylevel — какой уровень сейчас стоит\n"
    "• /time 09:00 — во сколько присылать урок\n"
    "• /translation 18:00 — во сколько присылать задание на перевод\n"
    "• /timezone Europe/Moscow — твой часовой пояс\n"
    "• /pause и /resume — приостановить и продолжить уроки\n"
    "• /status — текущие настройки"
)
_BAD_TIME = "Напиши время в формате ЧЧ:ММ, например: {command} 09:30"
_BAD_TIMEZONE = "Не знаю такой часовой пояс. Пример: /timezone Europe/Moscow или /timezone Asia/Vladivostok"
_PAUSED = "Уроки на паузе. Продолжить — /resume."
_RESUMED = "Уроки продолжаются с той темы, на которой остановились."
_GRADING = "⏳ Проверяю…"
_GRADE_FAILED = "Не получилось проверить перевод. Пришли ответ ещё раз чуть позже."
_DISPUTE_PREFIX = "dispute:"
_DISPUTED = "Спасибо, оценка отмечена как спорная."

log = logging.getLogger(__name__)

Grader = Callable[[Lesson, str], TranslationGrade]
Background = Callable[[Callable[[], None]], object]


def _run_now(task: Callable[[], None]) -> None:
    task()


@dataclass(frozen=True)
class PracticeHandlers:
    """What the bot needs for practice: the store, the grader and where to run grading."""

    store: PracticeStore
    grade: Grader
    background: Background = _run_now
    profiles: ProfileStore | None = None  # refreshed after a finished quiz or a graded translation


def _refresh_profile(practice: PracticeHandlers, learner_id: int) -> None:
    """The profile is derived data: a failure here is logged and never reaches the learner."""
    if practice.profiles is None:
        return
    try:
        refresh_profile(practice.profiles, learner_id, datetime.now(UTC))
    except SQLAlchemyError as error:
        log.warning("profile refresh failed for learner %s: %s", learner_id, error)


# Shown in Telegram's `/` command menu (registered via setMyCommands on startup).
_COMMANDS: list[tuple[str, str]] = [
    ("level", "Выбрать или сменить уровень"),
    ("mylevel", "Показать текущий уровень"),
    ("time", "Во сколько присылать урок"),
    ("translation", "Во сколько присылать задание на перевод"),
    ("timezone", "Часовой пояс"),
    ("pause", "Приостановить уроки"),
    ("resume", "Продолжить уроки"),
    ("status", "Текущие настройки"),
]


def render_status(learner: LearnerRecord) -> str:
    state = "на паузе" if learner.paused_at is not None else "идут" if learner.is_active else "остановлены (/start)"
    return (
        f"Уровень: <b>{learner.cefr}</b>\n"
        f"Часовой пояс: {learner.timezone}\n"
        f"Урок: {learner.lesson_send_time:%H:%M}, перевод: {learner.translation_send_time:%H:%M}\n"
        f"Уроки: {state}"
    )


def _change_schedule(
    learners: LearnerStore, chat_id: str, default_cefr: Cefr, default_schedule: LearnerSchedule, **changes: object
) -> LearnerRecord:
    learner = learners.get_or_create(chat_id, default_cefr, default_schedule)
    schedule = LearnerSchedule.model_validate({**learner.schedule.model_dump(), **changes})
    updated = learners.set_schedule(chat_id, schedule)
    assert updated is not None
    return updated


def _handle_schedule_command(
    client: TelegramClient,
    chat_id: str,
    command: str,
    argument: str,
    *,
    learners: LearnerStore,
    default_cefr: Cefr,
    default_schedule: LearnerSchedule,
) -> None:
    if command in {"/time", "/translation"}:
        clock = parse_clock(argument)
        if clock is None:
            client.send_message(chat_id, _BAD_TIME.format(command=command))
            return
        field = "lesson_send_time" if command == "/time" else "translation_send_time"
        learner = _change_schedule(learners, chat_id, default_cefr, default_schedule, **{field: clock})
    elif command == "/timezone":
        try:
            zone = valid_timezone(argument.strip())
        except ValueError:
            client.send_message(chat_id, _BAD_TIMEZONE)
            return
        learner = _change_schedule(learners, chat_id, default_cefr, default_schedule, timezone=zone)
    elif command in {"/pause", "/resume"}:
        learners.get_or_create(chat_id, default_cefr, default_schedule)
        changed = learners.set_paused(chat_id, command == "/pause")
        assert changed is not None
        client.send_message(chat_id, _PAUSED if command == "/pause" else _RESUMED)
        return
    else:  # /status
        learner = learners.get_or_create(chat_id, default_cefr, default_schedule)
    client.send_message(chat_id, render_status(learner))


_SCHEDULE_COMMANDS = {"/time", "/translation", "/timezone", "/pause", "/resume", "/status"}


def _keyboard(available: list[str]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": level, "callback_data": f"level:{level}"} for level in available]]}


def _dispute_keyboard(answer_id: int) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "Не согласна с оценкой", "callback_data": f"{_DISPUTE_PREFIX}{answer_id}"}]]}


def _grade_answer(
    client: TelegramClient,
    practice: PracticeHandlers,
    *,
    chat_id: str,
    message_id: int,
    answer_id: int,
    lesson: Lesson,
    text: str,
) -> None:
    """Runs in the background: grade, store, replace the progress message with the result."""
    try:
        try:
            grade = practice.grade(lesson, text)
        except Exception as error:  # the learner must hear back whatever went wrong
            log.exception("grading failed: %s: %s", type(error).__name__, error)
            practice.store.fail_translation_answer(answer_id, f"{type(error).__name__}: {error}")
            client.edit_message_text(chat_id, message_id, _GRADE_FAILED)
            return
        graded = practice.store.finish_translation_answer(answer_id, grade)
        client.edit_message_text(chat_id, message_id, render_grade(grade), reply_markup=_dispute_keyboard(answer_id))
    except (TelegramError, SQLAlchemyError) as error:
        log.warning("could not deliver the grade: %s", error)
        return
    _refresh_profile(practice, graded.learner_id)


def _take_translation_answer(client: TelegramClient, chat_id: str, text: str, practice: PracticeHandlers) -> bool:
    """Treat `text` as the answer to the open translation task; False when there is none."""
    task = practice.store.open_translation(chat_id)
    if task is None:
        return False
    answer = practice.store.start_translation_answer(task.delivery.id, task.learner_id, text)
    if answer is None:
        return False
    answer_id, lesson = answer.id, task.lesson
    message_id = client.send_message(chat_id, _GRADING)
    practice.store.set_progress_message(answer_id, message_id)
    practice.background(
        lambda: _grade_answer(
            client, practice, chat_id=chat_id, message_id=message_id, answer_id=answer_id, lesson=lesson, text=text
        )
    )
    return True


def _handle_poll_answer(client: TelegramClient, poll_answer: dict[str, Any], practice: PracticeHandlers) -> None:
    option_ids = [int(option) for option in poll_answer.get("option_ids") or []]
    user = poll_answer.get("user") or {}
    if not option_ids or user.get("id") is None:
        return  # a retracted vote, or an answer on behalf of a chat
    # In a private chat the chat id equals the user id.
    chat_id = str(user["id"])
    result = practice.store.record_poll_answer(str(poll_answer["poll_id"]), chat_id, option_ids)
    if result is not None and result.completed:
        client.send_message(chat_id, render_quiz_summary(result.results))
        _refresh_profile(practice, result.learner_id)


def _handle_message(
    client: TelegramClient,
    message: dict[str, Any],
    *,
    learners: LearnerStore,
    available: list[str],
    default_cefr: Cefr,
    default_schedule: LearnerSchedule,
    practice: PracticeHandlers | None = None,
) -> None:
    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    head, _, argument = text.partition(" ")
    command = head.split("@", 1)[0].lower() if text else ""

    if command in {"/start", "/level"}:
        if command == "/start":
            learner = learners.get_or_create(chat_id, default_cefr, default_schedule)
            learners.start_course(chat_id, local_today(learner.timezone, datetime.now(UTC)))
        client.send_message(chat_id, _PROMPT, reply_markup=_keyboard(available))
    elif command in _SCHEDULE_COMMANDS:
        _handle_schedule_command(
            client,
            chat_id,
            command,
            argument,
            learners=learners,
            default_cefr=default_cefr,
            default_schedule=default_schedule,
        )
    elif command == "/mylevel":
        learner = learners.get(chat_id)
        current = learner.cefr if learner is not None else default_cefr
        client.send_message(chat_id, f"Текущий уровень: <b>{current}</b>. Сменить — /level.")
    elif (
        practice is not None
        and text
        and not text.startswith("/")
        and _take_translation_answer(client, chat_id, text, practice)
    ):
        return
    else:
        client.send_message(chat_id, _HELP)


def _handle_callback(
    client: TelegramClient,
    callback: dict[str, Any],
    *,
    learners: LearnerStore,
    available: list[str],
    practice: PracticeHandlers | None = None,
) -> None:
    callback_id = str(callback["id"])
    data = str(callback.get("data") or "")
    if data.startswith(_DISPUTE_PREFIX) and practice is not None:
        answer_id = data.removeprefix(_DISPUTE_PREFIX)
        disputed = answer_id.isdigit() and practice.store.dispute(int(answer_id))
        client.answer_callback_query(callback_id, text=_DISPUTED if disputed else None)
        return
    if not data.startswith("level:"):
        client.answer_callback_query(callback_id)
        return

    chosen = data.split(":", 1)[1]
    if chosen not in available:
        client.answer_callback_query(callback_id, text="Этот уровень пока недоступен.")
        return

    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    # In a private chat the chat id equals the user id, so `from` covers a callback without a message.
    chat_id = chat.get("id", (callback.get("from") or {}).get("id"))
    if chat_id is None:
        client.answer_callback_query(callback_id)
        return

    learner = learners.set_cefr(str(chat_id), cast(Cefr, chosen))
    # Picking a level also starts the course, or restarts it after the previous level was finished.
    learners.start_course(learner.telegram_chat_id, local_today(learner.timezone, datetime.now(UTC)))
    client.answer_callback_query(callback_id, text=f"Уровень обновлён: {chosen}")

    if chat.get("id") is not None and message.get("message_id") is not None:
        client.edit_message_text(
            str(chat["id"]),
            int(message["message_id"]),
            f"✅ Твой уровень: <b>{chosen}</b>.\nПоменять в любой момент — /level.",
        )


def process_updates(
    client: TelegramClient,
    updates: list[dict[str, Any]],
    *,
    learners: LearnerStore,
    available: list[str],
    default_cefr: Cefr,
    default_schedule: LearnerSchedule | None = None,
    practice: PracticeHandlers | None = None,
) -> int | None:
    """Handle a batch of updates; returns the next getUpdates offset (or None if empty)."""
    schedule = default_schedule or LearnerSchedule()
    last_update_id: int | None = None
    for update in updates:
        last_update_id = int(update["update_id"])
        if "callback_query" in update:
            _handle_callback(
                client, update["callback_query"], learners=learners, available=available, practice=practice
            )
        elif "poll_answer" in update:
            if practice is not None:
                _handle_poll_answer(client, update["poll_answer"], practice)
        elif update.get("message", {}).get("chat"):
            _handle_message(
                client,
                update["message"],
                learners=learners,
                available=available,
                default_cefr=default_cefr,
                default_schedule=schedule,
                practice=practice,
            )
    return None if last_update_id is None else last_update_id + 1


def make_grader(settings: Settings) -> Grader:
    """Grade with GRADER_MODEL at GRADER_EFFORT; the client is created on first use."""
    llm: AnthropicLLM | None = None

    def grade(lesson: Lesson, text: str) -> TranslationGrade:
        nonlocal llm
        if llm is None:
            llm = AnthropicLLM(settings, model=settings.grader_model, effort=settings.grader_effort)
        return grade_translation(llm, lesson, text)

    return grade


def serve_leveling(settings: Settings, stores: Stores) -> int:
    """Long-poll the bot until Ctrl+C / SIGTERM (raised as KeyboardInterrupt); returns an exit code."""
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 2

    available = available_cefr_levels()
    offset: int | None = None
    # One worker: grading is rare, and answers are then graded in the order they came.
    with httpx.Client(timeout=_LONG_POLL_SECONDS + 15) as http, ThreadPoolExecutor(1, "grader") as executor:
        client = TelegramClient(http, token)
        practice = PracticeHandlers(
            stores.practice, make_grader(settings), background=executor.submit, profiles=stores.profiles
        )
        try:
            client.set_my_commands(_COMMANDS)  # make /level and /mylevel show in the bot's menu
        except TelegramError as error:
            log.warning("could not register commands: %s", error)
        log.info("listening for updates; available levels: %s", ", ".join(available))
        try:
            while True:
                try:
                    updates = client.get_updates(offset, timeout=_LONG_POLL_SECONDS, allowed_updates=_ALLOWED_UPDATES)
                    next_offset = process_updates(
                        client,
                        updates,
                        learners=stores.learners,
                        available=available,
                        default_cefr=settings.learner_cefr,
                        default_schedule=settings.default_schedule(),
                        practice=practice,
                    )
                except (TelegramError, SQLAlchemyError) as error:
                    # A long-running process must outlive network blips and database restarts.
                    log.warning("polling error, retrying in %ss: %s", _RETRY_SECONDS, error)
                    time.sleep(_RETRY_SECONDS)
                    continue
                if next_offset is not None:
                    offset = next_offset
        except KeyboardInterrupt:
            log.info("stopped")
            return 0


def run_leveling(settings: Settings) -> int:
    """Only the /level bot, without the daily scheduler (local debugging)."""
    stores = open_stores(settings)
    if stores is None:
        log.error("DATABASE_URL is not set")
        return 2
    return serve_leveling(settings, stores)
