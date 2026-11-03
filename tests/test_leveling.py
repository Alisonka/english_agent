from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from english_agent.config import Settings
from english_agent.learner import LearnerState, load_learner_state, save_learner_state
from english_agent.models.curriculum import load_curriculum
from english_agent.paths import available_cefr_levels
from english_agent.pipeline import resolve_cefr
from english_agent.telegram import TelegramClient
from english_agent.telegram.leveling import process_updates

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


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {"learner_state_path": tmp_path / "learner.json"}
    return Settings(_env_file=None, **(base | overrides))


# --- learner state ----------------------------------------------------------


def test_learner_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "learner.json"
    assert load_learner_state(path) is None
    save_learner_state(LearnerState(cefr="B1"), path)
    assert load_learner_state(path) == LearnerState(cefr="B1")


def test_learner_state_invalid_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "learner.json"
    path.write_text('{"cefr": "A1"}', encoding="utf-8")  # A1 is outside the supported range
    assert load_learner_state(path) is None


# --- level resolution -------------------------------------------------------


def test_resolve_cefr_prefers_explicit(tmp_path: Path) -> None:
    save_learner_state(LearnerState(cefr="B1"), tmp_path / "learner.json")
    assert resolve_cefr("B2", settings(tmp_path)) == "B2"


def test_resolve_cefr_uses_saved_choice(tmp_path: Path) -> None:
    save_learner_state(LearnerState(cefr="B1"), tmp_path / "learner.json")
    assert resolve_cefr(None, settings(tmp_path, learner_cefr="A2")) == "B1"


def test_resolve_cefr_falls_back_to_default(tmp_path: Path) -> None:
    assert resolve_cefr(None, settings(tmp_path, learner_cefr="A2")) == "A2"


# --- interactive handler ----------------------------------------------------


def _update(update_id: int, **content: Any) -> dict[str, Any]:
    return {"update_id": update_id, **content}


def test_level_command_offers_available_levels(tmp_path: Path) -> None:
    fake = RecordingTelegram()
    updates = [_update(1, message={"chat": {"id": 42}, "text": "/level"})]

    offset = process_updates(
        fake.client(), updates, state_path=tmp_path / "learner.json", available=["A2", "B1"], default_cefr="A2"
    )

    assert offset == 2
    method, payload = fake.requests[0]
    assert method == "sendMessage"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["level:A2", "level:B1"]


def test_callback_saves_level_and_confirms(tmp_path: Path) -> None:
    fake = RecordingTelegram()
    state_path = tmp_path / "learner.json"
    callback = {"id": "cq1", "data": "level:B1", "message": {"chat": {"id": 42}, "message_id": 7}}
    updates = [_update(5, callback_query=callback)]

    offset = process_updates(fake.client(), updates, state_path=state_path, available=["A2", "B1"], default_cefr="A2")

    assert offset == 6
    assert load_learner_state(state_path) == LearnerState(cefr="B1")
    methods = [method for method, _ in fake.requests]
    assert methods == ["answerCallbackQuery", "editMessageText"]


def test_callback_rejects_unavailable_level(tmp_path: Path) -> None:
    fake = RecordingTelegram()
    state_path = tmp_path / "learner.json"
    callback = {"id": "cq1", "data": "level:A1", "message": {"chat": {"id": 42}, "message_id": 7}}

    process_updates(
        fake.client(),
        [_update(1, callback_query=callback)],
        state_path=state_path,
        available=["A2", "B1"],
        default_cefr="A2",
    )

    assert load_learner_state(state_path) is None
    assert [method for method, _ in fake.requests] == ["answerCallbackQuery"]


def test_empty_batch_keeps_offset_none(tmp_path: Path) -> None:
    fake = RecordingTelegram()
    assert (
        process_updates(fake.client(), [], state_path=tmp_path / "x.json", available=["A2"], default_cefr="A2") is None
    )
    assert fake.requests == []


# --- curriculum selection ---------------------------------------------------


def test_available_levels_match_curriculum_files() -> None:
    assert available_cefr_levels() == ["A2", "B1", "B2", "C1"]


def test_load_curriculum_by_level() -> None:
    for level in ("A2", "B1", "B2", "C1"):
        assert load_curriculum(level).cefr == level
