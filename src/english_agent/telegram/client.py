"""Thin Telegram Bot API client over httpx. Only what the pipeline needs.

Errors never include the request URL, because the URL embeds the bot token.
"""

from __future__ import annotations

from typing import Any

import httpx

from english_agent.models.lesson import Lesson
from english_agent.telegram.render import render_lesson, split_message
from english_agent.tracing import get_tracer

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """Telegram rejected a request or could not be reached."""


class TelegramClient:
    def __init__(self, http: httpx.Client, token: str, *, api_base: str = TELEGRAM_API_BASE) -> None:
        if not token:
            raise ValueError("telegram bot token is empty")
        self._http = http
        self._base = f"{api_base.rstrip('/')}/bot{token}"

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.post(f"{self._base}/{method}", json=payload or {})
        except httpx.HTTPError as error:
            raise TelegramError(f"{method}: {type(error).__name__}: {error}") from error
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description") or f"HTTP {response.status_code}"
            raise TelegramError(f"{method} failed: {description}")
        return body["result"]

    def send_message(self, chat_id: str, text: str, *, parse_mode: str = "HTML") -> int:
        """Send one message; returns Telegram's message_id."""
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "link_preview_options": {"is_disabled": True},
            },
        )
        return int(result["message_id"])

    def send_lesson(self, chat_id: str, lesson: Lesson) -> list[int]:
        """Render the lesson and send it, split into as many messages as needed, in order."""
        with get_tracer().start_as_current_span("telegram.send_lesson") as span:
            chunks = split_message(render_lesson(lesson))
            span.set_attribute("telegram.messages", len(chunks))
            return [self.send_message(chat_id, chunk) for chunk in chunks]

    def known_chats(self) -> list[tuple[str, str]]:
        """(chat id, label) for every chat that appears in the bot's pending updates.

        Telegram only lets a bot write to users who have messaged it first, so this
        is how a user discovers their own chat id after sending /start to the bot.
        """
        seen: dict[str, str] = {}
        for update in self._call("getUpdates"):
            message = update.get("message") or update.get("edited_message") or update.get("channel_post") or {}
            chat = message.get("chat")
            if not chat:
                continue
            label = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            if chat.get("username"):
                label = f"{label} (@{chat['username']})".strip()
            seen[str(chat["id"])] = f"{label or '?'} [{chat.get('type', '?')}]"
        return list(seen.items())
