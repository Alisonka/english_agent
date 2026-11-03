"""Telegram helpers for one-time setup.

uv run python -m english_agent.telegram chats   # chats that have messaged the bot (to find TELEGRAM_CHAT_ID)
uv run python -m english_agent.telegram ping    # send a test message to TELEGRAM_CHAT_ID
uv run python -m english_agent.telegram level    # let the learner pick/change their CEFR level (long polling)
"""

from __future__ import annotations

import argparse
import sys

import httpx

from english_agent.config import get_settings
from english_agent.telegram.client import TelegramClient, TelegramError
from english_agent.telegram.leveling import run_leveling


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram setup helpers.")
    parser.add_argument("command", choices=["chats", "ping", "level"])
    args = parser.parse_args(argv)

    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2

    if args.command == "level":
        return run_leveling(settings)

    with httpx.Client(timeout=settings.http_timeout_seconds) as http:
        client = TelegramClient(http, token)
        try:
            if args.command == "chats":
                chats = client.known_chats()
                if not chats:
                    print("No chats yet: open your bot in Telegram, send it /start, then run this again.")
                    return 1
                for chat_id, label in chats:
                    print(f"{chat_id}\t{label}")
                return 0

            if not settings.telegram_chat_id:
                print("TELEGRAM_CHAT_ID is not set", file=sys.stderr)
                return 2
            message_id = client.send_message(settings.telegram_chat_id, "english-agent: test message")
            print(f"sent message {message_id} to chat {settings.telegram_chat_id}")
            return 0
        except TelegramError as error:
            print(f"telegram error: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
