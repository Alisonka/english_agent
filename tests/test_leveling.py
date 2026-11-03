from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from english_agent.models.curriculum import load_curriculum
from english_agent.models.storage import LearnerRecord
from english_agent.paths import available_cefr_levels
from english_agent.pipeline import resolve_cefr
from english_agent.telegram import TelegramClient
from english_agent.telegram.leveling import process_updates
from fakes import FakeLearners

TOKEN = "123:TOKEN"


class RecordingTelegram:
    """A TelegramClient whose calls are recorded and always answered ok."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def client(self) -> TelegramClient:
        return TelegramClient(httpx.Client(transport=httpx.MockTransport(self._handler)), TOKEN)


def learner(cefr: str) -> LearnerRecord:
    now = datetime.now(UTC)
    return LearnerRecord.model_validate(
        {"id": 1, "telegram_chat_id": "42", "cefr": cefr, "created_at": now, "updated_at": now}
    )


# --- level resolution -------------------------------------------------------


def test_resolve_cefr_prefers_explicit() -> None:
    assert resolve_cefr("B2", learner("B1"), "A2") == "B2"


def test_resolve_cefr_uses_saved_choice() -> None:
    assert resolve_cefr(None, learner("B1"), "A2") == "B1"


def test_resolve_cefr_falls_back_to_default() -> None:
    assert resolve_cefr(None, None, "A2") == "A2"


# --- interactive handler ----------------------------------------------------


def _update(update_id: int, **content: Any) -> dict[str, Any]:
    return {"update_id": update_id, **content}


def test_level_command_offers_available_levels() -> None:
    fake = RecordingTelegram()
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/level"})]

    offset = process_updates(fake.client(), updates, learners=FakeLearners(), available=["A2", "B1"], default_cefr="A2")

    assert offset == 2
    method, payload = fake.requests[0]
    assert method == "sendMessage"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["level:A2", "level:B1"]


def test_start_creates_learner_with_default_level() -> None:
    learners = FakeLearners()
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/start"})]

    process_updates(RecordingTelegram().client(), updates, learners=learners, available=["A2"], default_cefr="A2")

    created = learners.get("42")
    assert created is not None
    assert created.cefr == "A2"


def test_mylevel_reports_saved_level() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    learners.set_cefr("42", "B1")
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/mylevel"})]

    process_updates(fake.client(), updates, learners=learners, available=["A2", "B1"], default_cefr="A2")

    assert "<b>B1</b>" in fake.requests[0][1]["text"]


def test_callback_saves_level_and_confirms() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    callback = {"id": "cq1", "data": "level:B1", "message": {"chat": {"id": 42}, "message_id": 7}}
    updates = [_update(5, callback_query=callback)]

    offset = process_updates(fake.client(), updates, learners=learners, available=["A2", "B1"], default_cefr="A2")

    assert offset == 6
    saved = learners.get("42")
    assert saved is not None
    assert saved.cefr == "B1"
    methods = [method for method, _ in fake.requests]
    assert methods == ["answerCallbackQuery", "editMessageText"]


def test_callback_rejects_unavailable_level() -> None:
    fake = RecordingTelegram()
    learners = FakeLearners()
    callback = {"id": "cq1", "data": "level:A1", "message": {"chat": {"id": 42}, "message_id": 7}}

    process_updates(
        fake.client(),
        [_update(1, callback_query=callback)],
        learners=learners,
        available=["A2", "B1"],
        default_cefr="A2",
    )

    assert learners.get("42") is None
    assert [method for method, _ in fake.requests] == ["answerCallbackQuery"]


def test_empty_batch_keeps_offset_none() -> None:
    fake = RecordingTelegram()
    assert process_updates(fake.client(), [], learners=FakeLearners(), available=["A2"], default_cefr="A2") is None
    assert fake.requests == []


# --- curriculum selection ---------------------------------------------------


def test_available_levels_match_curriculum_files() -> None:
    assert available_cefr_levels() == ["A2", "B1", "B2", "C1"]


def test_load_curriculum_by_level() -> None:
    for level in ("A2", "B1", "B2", "C1"):
        assert load_curriculum(level).cefr == level
