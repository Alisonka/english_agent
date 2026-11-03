"""Let the learner pick and change their CEFR level from Telegram.

    uv run python -m english_agent.telegram level

A small long-polling loop over the sync `TelegramClient` (no aiogram yet): it
answers /level and /start with an inline keyboard of the levels that actually
have a curriculum, and writes the choice to the learner state file the daily
pipeline reads. Run it on a machine with a persistent filesystem (local or a
VPS); a stateless CI run instead uses the LEARNER_CEFR fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import httpx

from english_agent.config import Settings
from english_agent.learner import LearnerState, load_learner_state, save_learner_state
from english_agent.models.cefr import Cefr
from english_agent.paths import available_cefr_levels
from english_agent.telegram.client import TelegramClient, TelegramError

_LONG_POLL_SECONDS = 25

_PROMPT = "Выбери свой уровень английского. Поменять можно в любой момент командой /level."
_HELP = (
    "Я присылаю ежедневные уроки по твоему уровню.\n"
    "• /level — выбрать или сменить уровень\n"
    "• /mylevel — какой уровень сейчас стоит"
)

# Shown in Telegram's `/` command menu (registered via setMyCommands on startup).
_COMMANDS: list[tuple[str, str]] = [
    ("level", "Выбрать или сменить уровень"),
    ("mylevel", "Показать текущий уровень"),
]


def _keyboard(available: list[str]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": level, "callback_data": f"level:{level}"} for level in available]]}


def _handle_message(
    client: TelegramClient,
    message: dict[str, Any],
    *,
    state_path: Path,
    available: list[str],
    default_cefr: Cefr,
) -> None:
    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""

    if command in {"/start", "/level"}:
        client.send_message(chat_id, _PROMPT, reply_markup=_keyboard(available))
    elif command == "/mylevel":
        state = load_learner_state(state_path)
        current = state.cefr if state is not None else default_cefr
        client.send_message(chat_id, f"Текущий уровень: <b>{current}</b>. Сменить — /level.")
    else:
        client.send_message(chat_id, _HELP)


def _handle_callback(
    client: TelegramClient,
    callback: dict[str, Any],
    *,
    state_path: Path,
    available: list[str],
) -> None:
    callback_id = str(callback["id"])
    data = str(callback.get("data") or "")
    if not data.startswith("level:"):
        client.answer_callback_query(callback_id)
        return

    chosen = data.split(":", 1)[1]
    if chosen not in available:
        client.answer_callback_query(callback_id, text="Этот уровень пока недоступен.")
        return

    save_learner_state(LearnerState(cefr=cast(Cefr, chosen)), state_path)
    client.answer_callback_query(callback_id, text=f"Уровень обновлён: {chosen}")

    message = callback.get("message") or {}
    chat = message.get("chat") or {}
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
    state_path: Path,
    available: list[str],
    default_cefr: Cefr,
) -> int | None:
    """Handle a batch of updates; returns the next getUpdates offset (or None if empty)."""
    last_update_id: int | None = None
    for update in updates:
        last_update_id = int(update["update_id"])
        if "callback_query" in update:
            _handle_callback(client, update["callback_query"], state_path=state_path, available=available)
        elif update.get("message", {}).get("chat"):
            _handle_message(
                client,
                update["message"],
                state_path=state_path,
                available=available,
                default_cefr=default_cefr,
            )
    return None if last_update_id is None else last_update_id + 1


def run_leveling(settings: Settings) -> int:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2

    available = available_cefr_levels()
    offset: int | None = None
    with httpx.Client(timeout=_LONG_POLL_SECONDS + 15) as http:
        client = TelegramClient(http, token)
        try:
            client.set_my_commands(_COMMANDS)  # make /level and /mylevel show in the bot's menu
        except TelegramError as error:
            print(f"could not register commands: {error}", file=sys.stderr)
        print(
            f"Listening for /level — available levels: {', '.join(available)}. Press Ctrl+C to stop.",
            file=sys.stderr,
        )
        try:
            while True:
                updates = client.get_updates(offset, timeout=_LONG_POLL_SECONDS)
                next_offset = process_updates(
                    client,
                    updates,
                    state_path=settings.learner_state_path,
                    available=available,
                    default_cefr=settings.learner_cefr,
                )
                if next_offset is not None:
                    offset = next_offset
        except KeyboardInterrupt:
            print("stopped", file=sys.stderr)
            return 0
        except TelegramError as error:
            print(f"telegram error: {error}", file=sys.stderr)
            return 1
