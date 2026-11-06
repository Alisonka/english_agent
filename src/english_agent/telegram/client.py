"""Thin Telegram Bot API client over httpx. Only what the pipeline needs.

Errors never include the request URL, because the URL embeds the bot token.
"""

from __future__ import annotations

from typing import Any

import httpx

from english_agent.models.lesson import Lesson
from english_agent.telegram.render import render_messages
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

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        """Send one message; returns Telegram's message_id."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Replace the text (and optionally the keyboard) of a message we sent."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> None:
        """Acknowledge a button tap so Telegram stops its loading spinner."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def send_poll(
        self, chat_id: str, question: str, options: list[str], *, correct_option: int, explanation: str
    ) -> tuple[int, str]:
        """Send a quiz poll; returns (message_id, poll_id). Not anonymous, so answers arrive as `poll_answer`."""
        result = self._call(
            "sendPoll",
            {
                "chat_id": chat_id,
                "question": question,
                "options": [{"text": option} for option in options],
                "type": "quiz",
                "is_anonymous": False,
                "correct_option_ids": [correct_option],
                "explanation": explanation,
            },
        )
        return int(result["message_id"]), str(result["poll"]["id"])

    def get_updates(
        self, offset: int | None = None, *, timeout: int = 0, allowed_updates: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Pending updates. `timeout` > 0 long-polls; `offset` acks everything below it."""
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        return list(self._call("getUpdates", payload))

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register the bot's command menu (the list shown behind the `/` button)."""
        self._call(
            "setMyCommands",
            {"commands": [{"command": command, "description": description} for command, description in commands]},
        )

    def send_lesson(self, chat_id: str, lesson: Lesson) -> list[int]:
        """Render the lesson and send it, split into as many messages as needed, in order."""
        with get_tracer().start_as_current_span("telegram.send_lesson") as span:
            chunks = render_messages(lesson)
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
