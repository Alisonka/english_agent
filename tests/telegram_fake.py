"""A fake Bot API for aiogram: answers like Telegram, records every call, fails on demand.

Responses go through aiogram's own `check_response`, so a scripted 403 raises the real
`TelegramForbiddenError` and a 429 the real `TelegramRetryAfter`, and the request middlewares
(rate limits, flood retries) run exactly as in production.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, GetUpdates, SendMessage, SendPoll, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Update

from english_agent.config import Settings
from english_agent.telegram.sender import Sender, make_bot
from english_agent.telegram.throttle import RateLimiter

TOKEN = "123:TOKEN"
Failure = Callable[[TelegramMethod[Any]], tuple[int, dict[str, Any]] | None]


def blocked_by(chat_id: str) -> Failure:
    """A failure rule: every call to `chat_id` answers 403 like a bot the user blocked."""

    def rule(method: TelegramMethod[Any]) -> tuple[int, dict[str, Any]] | None:
        if str(getattr(method, "chat_id", "")) == chat_id:
            return 403, {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
        return None

    return rule


class NoLimit:
    async def acquire(self, chat_id: str | None) -> None:
        return None


class FakeTelegram(BaseSession):
    def __init__(self, *, fail: Failure | None = None) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.fail = fail
        self.updates: list[dict[str, Any]] = []
        self._next_id = 0

    # -- what tests look at ----------------------------------------------------------

    def of(self, kind: type[TelegramMethod[Any]]) -> list[Any]:
        return [call for call in self.calls if isinstance(call, kind)]

    @property
    def texts(self) -> list[str]:
        return [call.text for call in self.calls if isinstance(call, SendMessage | EditMessageText)]

    @property
    def chats(self) -> list[str]:
        return [str(call.chat_id) for call in self.of(SendMessage)]

    # -- the Bot API -----------------------------------------------------------------

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> Any:
        self.calls.append(method)
        failure = self.fail(method) if self.fail is not None else None
        if failure is not None:
            status, body = failure
            return self.check_response(bot, method, status, json.dumps({"ok": False, **body})).result
        result = self._result(method)
        return self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result})).result

    def _message(self, chat_id: Any, **extra: Any) -> dict[str, Any]:
        self._next_id += 1
        chat = {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else 1, "type": "private"}
        return {"message_id": self._next_id, "date": 0, "chat": chat, **extra}

    def _result(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, SendMessage):
            return self._message(method.chat_id, text=method.text)
        if isinstance(method, SendPoll):
            message = self._message(method.chat_id)
            message["poll"] = {
                "id": f"poll-{message['message_id']}",
                "question": method.question,
                "options": [
                    {"persistent_id": str(n), "text": getattr(option, "text", option), "voter_count": 0}
                    for n, option in enumerate(method.options)
                ],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": False,
                "type": "quiz",
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            }
            return message
        if isinstance(method, EditMessageText):
            return self._message(method.chat_id, text=method.text)
        if isinstance(method, GetUpdates):
            updates, self.updates = self.updates, []
            return updates
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None


def fake_bot(telegram: FakeTelegram, limiter: RateLimiter | None = None) -> Bot:
    """The production bot (defaults and middlewares) over the fake API, without waiting on rate limits."""
    settings = Settings(_env_file=None, telegram_bot_token=TOKEN)
    return make_bot(settings, session=telegram, limiter=limiter or NoLimit())


def fake_sender(telegram: FakeTelegram) -> Sender:
    return Sender(fake_bot(telegram))


def update(bot: Bot, update_id: int, **content: Any) -> Update:
    return Update.model_validate({"update_id": update_id, **content}, context={"bot": bot})
