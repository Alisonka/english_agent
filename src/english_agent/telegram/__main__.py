"""Telegram helpers for one-time setup.

uv run python -m english_agent.telegram chats   # chats that have messaged the bot (to find TELEGRAM_CHAT_ID)
uv run python -m english_agent.telegram ping    # send a test message to TELEGRAM_CHAT_ID

The bot itself runs inside `python -m english_agent.app`. `chats` reads pending updates, so
run it only while the app is stopped (a bot has one `getUpdates` consumer).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram.types import Update

from english_agent.config import Settings, get_settings
from english_agent.logs import configure_logging
from english_agent.telegram.sender import Sender, TelegramError, make_bot

log = logging.getLogger(__name__)


def chat_label(update: Update) -> tuple[str, str] | None:
    """(chat id, label) of the chat an update came from, if it has one."""
    message = update.message or update.edited_message or update.channel_post
    if message is None:
        return None
    chat = message.chat
    label = chat.title or " ".join(part for part in (chat.first_name, chat.last_name) if part)
    if chat.username:
        label = f"{label} (@{chat.username})".strip()
    return str(chat.id), f"{label or '?'} [{chat.type}]"


async def _run(settings: Settings, command: str) -> int:
    bot = make_bot(settings)
    try:
        if command == "chats":
            seen = dict(label for update in await bot.get_updates() if (label := chat_label(update)))
            if not seen:
                print("No chats yet: open your bot in Telegram, send it /start, then run this again.")
                return 1
            for chat_id, label in seen.items():
                print(f"{chat_id}\t{label}")
            return 0
        if not settings.telegram_chat_id:
            log.error("TELEGRAM_CHAT_ID is not set")
            return 2
        message_id = await Sender(bot).send_text(settings.telegram_chat_id, "english-agent: test message")
        print(f"sent message {message_id} to chat {settings.telegram_chat_id}")
        return 0
    except TelegramError as error:
        log.error("telegram error: %s", error)
        return 1
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram setup helpers.")
    parser.add_argument("command", choices=["chats", "ping"])
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    if not settings.telegram_bot_token.get_secret_value():
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 2
    return asyncio.run(_run(settings, args.command))


if __name__ == "__main__":
    raise SystemExit(main())
