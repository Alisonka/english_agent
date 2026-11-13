"""A fake Telegram Bot API for load tests: accepts what the bot sends, enforces Telegram's limits.

    python scripts/fake_telegram.py --port 8081        # then TELEGRAM_API_BASE=http://<host>:8081
    curl http://localhost:8081/stats                    # what each chat received

It answers sendMessage, sendPoll, editMessageText, answerCallbackQuery, setMyCommands,
deleteWebhook, getMe and getUpdates (which long-polls and returns nothing), and answers 429 with
`retry_after` when more than 30 messages a second, or more than one a second to one chat, arrive,
like Telegram does. `/stats` reports per-chat counts, duplicate texts and how many 429s it gave.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from typing import Any

from aiohttp import web

GLOBAL_PER_SECOND = 30
CHAT_PER_SECOND = 1
_SENDING = {"sendMessage", "sendPoll"}


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: dict[str, list[str]] = defaultdict(list)
        self.methods: Counter[str] = Counter()
        self.flood_waits = 0
        self._windows: Counter[tuple[str, int]] = Counter()
        self._next_id = 0

    def _over_limit(self, chat_id: str) -> bool:
        second = int(time.time())
        self._windows[("*", second)] += 1
        self._windows[(chat_id, second)] += 1
        return self._windows[("*", second)] > GLOBAL_PER_SECOND or self._windows[(chat_id, second)] > CHAT_PER_SECOND

    def _message(self, chat_id: str, **extra: Any) -> dict[str, Any]:
        self._next_id += 1
        chat = int(chat_id) if chat_id.lstrip("-").isdigit() else 1
        return {"message_id": self._next_id, "date": int(time.time()), "chat": {"id": chat, "type": "private"}, **extra}

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        self.methods[method] += 1
        params = await _params(request)
        chat_id = str(params.get("chat_id", ""))
        if method == "getUpdates":
            await asyncio.sleep(min(float(params.get("timeout", 0) or 0), 25))
            return _ok([])
        if method in _SENDING and self._over_limit(chat_id):
            self.flood_waits += 1
            body = {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 1},
            }
            return web.json_response(body, status=429)
        if method == "sendMessage":
            self.messages[chat_id].append(str(params.get("text", "")))
            return _ok(self._message(chat_id, text=params.get("text", "")))
        if method == "sendPoll":
            self.messages[chat_id].append(f"poll: {params.get('question', '')}")
            message = self._message(chat_id)
            options = params.get("options") or []
            message["poll"] = {
                "id": f"poll-{message['message_id']}",
                "question": params.get("question", ""),
                "options": [
                    {"persistent_id": str(n), "text": o.get("text", o) if isinstance(o, dict) else o, "voter_count": 0}
                    for n, o in enumerate(options)
                ],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": False,
                "type": "quiz",
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            }
            return _ok(message)
        if method == "editMessageText":
            return _ok(self._message(chat_id, text=params.get("text", "")))
        if method == "getMe":
            return _ok({"id": 1, "is_bot": True, "first_name": "fake", "username": "fake_bot"})
        return _ok(True)

    async def stats(self, request: web.Request) -> web.Response:
        duplicates = {chat: n for chat, texts in self.messages.items() if (n := len(texts) - len(set(texts)))}
        return web.json_response(
            {
                "chats": len(self.messages),
                "messages": {chat: len(texts) for chat, texts in self.messages.items()},
                "duplicates": duplicates,
                "flood_waits": self.flood_waits,
                "methods": dict(self.methods),
            }
        )


async def _params(request: web.Request) -> dict[str, Any]:
    """aiogram posts form data; nested values (options, reply_markup) arrive as JSON strings."""
    form = await request.post()
    params: dict[str, Any] = {}
    for key, value in form.items():
        text = value if isinstance(value, str) else ""
        try:
            params[key] = json.loads(text) if text[:1] in "[{" else text
        except ValueError:
            params[key] = text
    return params


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def make_app() -> web.Application:
    fake = FakeTelegram()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", fake.handle)
    app.router.add_get("/stats", fake.stats)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8081)
    web.run_app(make_app(), port=parser.parse_args().port)
