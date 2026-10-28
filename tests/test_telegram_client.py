from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from english_agent.models.lesson import Lesson
from english_agent.telegram import TelegramClient, TelegramError
from english_agent.telegram.render import render_lesson

TOKEN = "123456:SECRET-TOKEN"


class FakeTelegram:
    """Records Bot API calls and answers from a scripted queue of responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses = list(responses or [])
        self.next_message_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((method, json.loads(request.content or b"{}")))
        if self.responses:
            return self.responses.pop(0)
        self.next_message_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})

    def client(self) -> TelegramClient:
        return TelegramClient(httpx.Client(transport=httpx.MockTransport(self.handler)), TOKEN)


def test_requires_token() -> None:
    with pytest.raises(ValueError):
        TelegramClient(httpx.Client(), "")


def test_send_message_posts_html_to_bot_endpoint() -> None:
    fake = FakeTelegram()
    message_id = fake.client().send_message("42", "<b>hi</b>")

    assert message_id == 101
    method, payload = fake.requests[0]
    assert method == "sendMessage"
    assert payload == {
        "chat_id": "42",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


def test_token_goes_into_url_only() -> None:
    fake = FakeTelegram()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return fake.handler(request)

    TelegramClient(httpx.Client(transport=httpx.MockTransport(handler)), TOKEN).send_message("42", "x")
    assert seen_urls == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]
    assert TOKEN not in json.dumps(fake.requests[0][1])


def test_api_error_raises_without_leaking_token() -> None:
    fake = FakeTelegram(
        [httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})]
    )
    with pytest.raises(TelegramError) as info:
        fake.client().send_message("42", "x")
    assert "chat not found" in str(info.value)
    assert TOKEN not in str(info.value)


def test_non_json_server_error_raises() -> None:
    fake = FakeTelegram([httpx.Response(502, text="<html>bad gateway</html>")])
    with pytest.raises(TelegramError, match="HTTP 502"):
        fake.client().send_message("42", "x")


def test_ok_false_with_200_status_raises() -> None:
    fake = FakeTelegram([httpx.Response(200, json={"ok": False, "description": "nope"})])
    with pytest.raises(TelegramError, match="nope"):
        fake.client().send_message("42", "x")


def test_transport_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = TelegramClient(httpx.Client(transport=httpx.MockTransport(handler)), TOKEN)
    with pytest.raises(TelegramError, match="ConnectError"):
        client.send_message("42", "x")


def test_send_lesson_sends_rendered_text_as_one_message(lesson: Lesson) -> None:
    fake = FakeTelegram()
    ids = fake.client().send_lesson("42", lesson)

    assert ids == [101]
    assert [method for method, _ in fake.requests] == ["sendMessage"]
    assert fake.requests[0][1]["text"] == render_lesson(lesson)


def test_send_lesson_splits_long_lessons_in_order(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> None:
    import english_agent.telegram.client as client_module

    text = render_lesson(lesson)
    monkeypatch.setattr(
        client_module, "render_messages", lambda _lesson: [text[: len(text) // 2], text[len(text) // 2 :]]
    )
    fake = FakeTelegram()
    ids = fake.client().send_lesson("42", lesson)

    assert ids == [101, 102]
    sent = "".join(payload["text"] for _, payload in fake.requests)
    assert sent == render_lesson(lesson)


def test_send_lesson_stops_at_first_failure(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> None:
    import english_agent.telegram.client as client_module

    monkeypatch.setattr(client_module, "render_messages", lambda _lesson: ["a", "b", "c"])
    fake = FakeTelegram(
        [
            httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
            httpx.Response(429, json={"ok": False, "description": "Too Many Requests"}),
        ]
    )
    with pytest.raises(TelegramError, match="Too Many Requests"):
        fake.client().send_lesson("42", lesson)
    assert len(fake.requests) == 2


def test_known_chats_lists_each_chat_once() -> None:
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {"chat": {"id": 42, "type": "private", "first_name": "Alice", "username": "alice"}},
            },
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "first_name": "Alice"}}},
            {"update_id": 3, "channel_post": {"chat": {"id": -100, "type": "channel", "title": "News"}}},
            {"update_id": 4, "my_chat_member": {"chat": {"id": 7}}},
        ],
    }
    fake = FakeTelegram([httpx.Response(200, json=updates)])
    chats = fake.client().known_chats()

    assert fake.requests[0][0] == "getUpdates"
    assert chats == [("42", "Alice [private]"), ("-100", "News [channel]")]


def test_known_chats_empty_when_nobody_wrote_to_the_bot() -> None:
    fake = FakeTelegram([httpx.Response(200, json={"ok": True, "result": []})])
    assert fake.client().known_chats() == []
