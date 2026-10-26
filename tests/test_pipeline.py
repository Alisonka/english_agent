from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from english_agent import pipeline
from english_agent.config import Settings
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.research.models import ResearchResult
from english_agent.telegram import TelegramError


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture
def telegram_settings() -> Settings:
    return make_settings(telegram_bot_token="123:TOKEN", telegram_chat_id="42")


@pytest.fixture
def sent() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]) -> None:
    """Route every pipeline HTTP client through a fake Telegram server."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    monkeypatch.setattr(pipeline, "_http_client", lambda settings: httpx.Client(transport=httpx.MockTransport(handler)))


def research_result(topic_id: str, status: str = "failed", message: str = "boom") -> ResearchResult:
    """A research outcome without notes; `generate_lesson` is stubbed, so the lesson comes separately."""
    return ResearchResult.model_validate(
        {"topic_id": topic_id, "status": status, "message": message, "sites_checked": [], "steps": 0, "trajectory": []}
    )


def test_deliver_lesson_sends_to_configured_chat(
    lesson: Lesson, telegram_settings: Settings, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    assert pipeline.deliver_lesson(lesson, telegram_settings) == [1]
    assert sent[0]["chat_id"] == "42"
    assert lesson.title_en in sent[0]["text"]


def test_deliver_lesson_requires_configuration(lesson: Lesson) -> None:
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        pipeline.deliver_lesson(lesson, make_settings(telegram_bot_token="123:TOKEN"))


def test_main_unknown_topic(capsys: pytest.CaptureFixture[str]) -> None:
    assert pipeline.main(["--topic", "no_such_topic"]) == 2
    assert "unknown topic" in capsys.readouterr().err


def test_main_refuses_to_run_unconfigured_before_calling_llm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)

    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("generate_lesson must not be called")

    monkeypatch.setattr(pipeline, "generate_lesson", must_not_run)
    assert pipeline.main(["--topic", "there_is_are"]) == 2
    assert "--no-send" in capsys.readouterr().err


def test_main_no_send_prints_lesson_without_telegram(
    lesson: Lesson,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))

    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 0
    assert json.loads(capsys.readouterr().out)["topic_id"] == "there_is_are"
    assert sent == []


def test_main_sends_lesson_and_reports(
    lesson: Lesson,
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))

    assert pipeline.main(["--topic", "there_is_are"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["topic_id"] == "there_is_are"
    assert "sent 1 message(s) to Telegram chat 42" in captured.err
    assert len(sent) == 1


def test_main_reports_telegram_failure(
    lesson: Lesson,
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

    monkeypatch.setattr(pipeline, "_http_client", lambda s: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "bot was blocked" in capsys.readouterr().err


def test_main_when_research_finds_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (result, None))

    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 1
    assert "нет на сайте" in capsys.readouterr().err


def test_main_notifies_chat_when_research_finds_nothing(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (result, None))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert len(sent) == 1
    assert "не получился: нет на сайте" in sent[0]["text"]


def test_main_notifies_chat_when_generation_raises(
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("API <down> & out")

    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", boom)

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "RuntimeError: API <down> & out" in capsys.readouterr().err
    assert "RuntimeError: API &lt;down&gt; &amp; out" in sent[0]["text"]


def test_main_failure_notice_that_cannot_be_delivered_is_logged(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

    monkeypatch.setattr(pipeline, "_http_client", lambda s: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result("there_is_are"), None))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    err = capsys.readouterr().err
    assert "lesson for there_is_are failed" in err
    assert "bot was blocked" in err


# --- topic of the day -------------------------------------------------------

START = date(2026, 11, 1)


def test_topic_for_day_follows_curriculum_order() -> None:
    curriculum = load_curriculum()
    ordered = curriculum.topic_ids()

    days = [0, 1, len(ordered) - 1, len(ordered)]
    picked = [pipeline.topic_for_day(curriculum, day) for day in days]
    assert [topic.id if topic else None for topic in picked] == [ordered[0], ordered[1], ordered[-1], None]
    with pytest.raises(ValueError):
        pipeline.topic_for_day(curriculum, -1)


@pytest.fixture
def generated(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub generate_lesson; collects the topic ids it was asked for."""
    topics: list[str] = []

    def fake_generate(topic: Any, settings: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        topics.append(topic.id)
        return research_result(topic.id), lesson

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)
    return topics


def scheduled(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "curriculum_start_date": START,
        "telegram_bot_token": "123:TOKEN",
        "telegram_chat_id": "42",
    }
    settings = make_settings(**(values | overrides))
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)


def test_main_picks_topic_of_the_day(
    monkeypatch: pytest.MonkeyPatch, generated: list[str], fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    scheduled(monkeypatch)
    ordered = load_curriculum().topic_ids()

    assert pipeline.main(["--date", "2026-11-01"]) == 0
    assert pipeline.main(["--date", "2026-11-03"]) == 0
    assert generated == [ordered[0], ordered[2]]
    assert len(sent) == 2


def test_main_uses_today_by_default(monkeypatch: pytest.MonkeyPatch, generated: list[str]) -> None:
    scheduled(monkeypatch, curriculum_start_date=date.today())
    assert pipeline.main(["--no-send"]) == 0
    assert generated == [load_curriculum().topic_ids()[0]]


def test_main_explicit_topic_overrides_schedule(monkeypatch: pytest.MonkeyPatch, generated: list[str]) -> None:
    scheduled(monkeypatch)
    assert pipeline.main(["--topic", "there_is_are", "--date", "2026-11-01", "--no-send"]) == 0
    assert generated == ["there_is_are"]


def test_main_requires_start_date_without_topic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], generated: list[str]
) -> None:
    scheduled(monkeypatch, curriculum_start_date=None)
    assert pipeline.main(["--no-send"]) == 2
    assert "CURRICULUM_START_DATE" in capsys.readouterr().err
    assert generated == []


def test_main_before_course_start_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    generated: list[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    scheduled(monkeypatch)
    assert pipeline.main(["--date", "2026-10-31"]) == 0
    assert "course starts on 2026-11-01" in capsys.readouterr().out
    assert generated == []
    assert sent == []


def test_main_course_finished_is_announced_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    generated: list[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    scheduled(monkeypatch)
    total = len(load_curriculum().topics)
    first_day_after = START + timedelta(days=total)

    assert pipeline.main(["--date", first_day_after.isoformat()]) == 0
    assert pipeline.main(["--date", (first_day_after + timedelta(days=1)).isoformat()]) == 0
    assert capsys.readouterr().out.count(pipeline.COURSE_FINISHED) == 2
    assert generated == []
    assert [message["text"] for message in sent] == [pipeline.COURSE_FINISHED]


def test_main_course_finished_with_no_send_stays_silent(
    monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    scheduled(monkeypatch)
    day = START + timedelta(days=len(load_curriculum().topics))
    assert pipeline.main(["--date", day.isoformat(), "--no-send"]) == 0
    assert sent == []


def test_main_rejects_malformed_date(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled(monkeypatch)
    with pytest.raises(SystemExit) as info:
        pipeline.main(["--date", "01.11.2026"])
    assert info.value.code == 2


@pytest.mark.parametrize(("raw", "expected"), [("2026-11-01", START), ("", None)])
def test_start_date_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: date | None) -> None:
    monkeypatch.setenv("CURRICULUM_START_DATE", raw)
    assert make_settings().curriculum_start_date == expected
