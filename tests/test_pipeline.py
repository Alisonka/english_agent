from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from english_agent import pipeline
from english_agent.config import Settings
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord
from english_agent.research.models import ResearchResult
from english_agent.storage import Stores, open_stores
from english_agent.telegram import TelegramError
from fakes import FakeLearners, FakeLessons, fake_items, fake_stores


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def stores(monkeypatch: pytest.MonkeyPatch) -> Stores:
    """Every `main()` run records into in-memory repositories instead of PostgreSQL."""
    fakes, _, _ = fake_stores()
    monkeypatch.setattr(pipeline, "open_stores", lambda settings: fakes)
    return fakes


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


def test_deliver_lesson_sends_to_the_chat(lesson: Lesson, fake_telegram: None, sent: list[dict[str, Any]]) -> None:
    assert pipeline.deliver_lesson(lesson, make_settings(telegram_bot_token="123:TOKEN"), "42") == [1]
    assert sent[0]["chat_id"] == "42"
    assert lesson.title_en in sent[0]["text"]


def test_deliver_lesson_requires_a_token(lesson: Lesson) -> None:
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        pipeline.deliver_lesson(lesson, make_settings(), "42")


def test_main_unknown_topic(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    assert pipeline.main(["--topic", "no_such_topic"]) == 2
    assert "unknown topic" in caplog.text


def test_main_refuses_to_run_unconfigured_before_calling_llm(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)

    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("generate_lesson must not be called")

    monkeypatch.setattr(pipeline, "generate_lesson", must_not_run)
    assert pipeline.main(["--topic", "there_is_are"]) == 2
    assert "--no-send" in caplog.text


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
    caplog: pytest.LogCaptureFixture,
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))

    assert pipeline.main(["--topic", "there_is_are"]) == 0
    assert "sent 1 message(s) to Telegram chat 42" in caplog.text
    assert len(sent) == 1


def test_main_reports_telegram_failure_and_stops_writing_to_a_blocked_chat(
    lesson: Lesson,
    telegram_settings: Settings,
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

    monkeypatch.setattr(pipeline, "_http_client", lambda s: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "bot was blocked" in caplog.text
    learner = stores.learners.get("42")
    assert learner is not None and learner.deactivated_reason == "blocked"


def test_main_when_research_finds_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (result, None))

    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 1
    assert "нет на сайте" in caplog.text


def test_main_tells_the_learner_when_research_finds_nothing(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (result, None))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    [notice] = sent
    assert "не получился" in notice["text"] and "завтра" in notice["text"]


def test_main_keeps_error_details_out_of_the_chat(
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("API <down> & out")

    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", boom)

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "RuntimeError: API <down> & out" in caplog.text
    assert "API" not in sent[0]["text"]


def test_main_failure_notice_that_cannot_be_delivered_is_logged(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

    monkeypatch.setattr(pipeline, "_http_client", lambda s: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result("there_is_are"), None))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "lesson for there_is_are failed" in caplog.text
    assert "bot was blocked" in caplog.text


# --- topic selection ---------------------------------------------------------

START = date(2026, 11, 1)


def test_topic_for_day_follows_curriculum_order() -> None:
    curriculum = load_curriculum()
    ordered = curriculum.topic_ids()

    days = [0, 1, len(ordered) - 1, len(ordered)]
    picked = [pipeline.topic_for_day(curriculum, day) for day in days]
    assert [topic.id if topic else None for topic in picked] == [ordered[0], ordered[1], ordered[-1], None]
    with pytest.raises(ValueError):
        pipeline.topic_for_day(curriculum, -1)


def test_next_topic_is_the_first_one_not_sent_yet() -> None:
    curriculum = load_curriculum()
    ordered = curriculum.topic_ids()

    first = pipeline.next_topic(curriculum, set())
    assert first is not None and first.id == ordered[0]
    picked = pipeline.next_topic(curriculum, {ordered[0], ordered[2]})
    assert picked is not None and picked.id == ordered[1]  # a gap (a failed day) is filled first
    assert pipeline.next_topic(curriculum, set(ordered)) is None


@pytest.fixture
def generated(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub generate_lesson; collects the topic ids it was asked for. The lesson keeps the asked topic id."""
    topics: list[str] = []

    def fake_generate(topic: Any, settings: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        topics.append(topic.id)
        return research_result(topic.id), lesson.model_copy(update={"topic_id": topic.id})

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)
    return topics


def preview(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values: dict[str, Any] = {"curriculum_start_date": START}
    settings = make_settings(**(values | overrides))
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)


def test_main_sends_the_learners_next_topic(
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    generated: list[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    ordered = load_curriculum().topic_ids()

    assert pipeline.main(["--date", "2026-11-01"]) == 0
    assert pipeline.main(["--date", "2026-11-03"]) == 0  # a day without a lesson in between loses no topic
    assert generated == [ordered[0], ordered[1]]
    assert len(sent) == 2


def test_main_no_send_previews_the_topic_of_today(monkeypatch: pytest.MonkeyPatch, generated: list[str]) -> None:
    preview(monkeypatch, curriculum_start_date=pipeline.local_today(make_settings()))
    assert pipeline.main(["--no-send"]) == 0
    assert generated == [load_curriculum().topic_ids()[0]]


def test_main_explicit_topic_overrides_the_preview_day(monkeypatch: pytest.MonkeyPatch, generated: list[str]) -> None:
    preview(monkeypatch)
    assert pipeline.main(["--topic", "there_is_are", "--date", "2026-11-01", "--no-send"]) == 0
    assert generated == ["there_is_are"]


def test_main_preview_requires_start_date_without_topic(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, generated: list[str]
) -> None:
    preview(monkeypatch, curriculum_start_date=None)
    assert pipeline.main(["--no-send"]) == 2
    assert "CURRICULUM_START_DATE" in caplog.text
    assert generated == []


def test_main_preview_before_course_start_does_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], generated: list[str]
) -> None:
    preview(monkeypatch)
    assert pipeline.main(["--date", "2026-10-31", "--no-send"]) == 0
    assert "course starts on 2026-11-01" in capsys.readouterr().out
    assert generated == []


def test_main_preview_after_the_last_topic_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_telegram: None,
    sent: list[dict[str, Any]],
) -> None:
    preview(monkeypatch)
    day = START + timedelta(days=len(load_curriculum().topics))
    assert pipeline.main(["--date", day.isoformat(), "--no-send"]) == 0
    assert pipeline.COURSE_FINISHED in capsys.readouterr().out
    assert sent == []


def test_main_rejects_malformed_date(monkeypatch: pytest.MonkeyPatch) -> None:
    preview(monkeypatch)
    with pytest.raises(SystemExit) as info:
        pipeline.main(["--date", "01.11.2026"])
    assert info.value.code == 2


@pytest.mark.parametrize(("raw", "expected"), [("2026-11-01", START), ("", None)])
def test_start_date_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: date | None) -> None:
    monkeypatch.setenv("CURRICULUM_START_DATE", raw)
    assert make_settings().curriculum_start_date == expected


# --- when is a learner's lesson due -------------------------------------------

MSK = ZoneInfo("Europe/Moscow")


def learner_record(**changes: Any) -> LearnerRecord:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    values: dict[str, Any] = {
        "id": 1,
        "telegram_chat_id": "42",
        "cefr": "A2",
        "created_at": now,
        "updated_at": now,
        "curriculum_started_on": START,
    }
    return LearnerRecord.model_validate(values | changes)


def msk(hour: int, minute: int = 0, day: date = START) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=MSK)


def test_lesson_is_due_from_send_time_until_the_catch_up_window_closes() -> None:
    settings, learner = make_settings(lesson_catch_up_hours=6), learner_record()
    assert not pipeline.lesson_due(learner, None, msk(8, 59), settings)
    assert pipeline.lesson_due(learner, None, msk(9, 0), settings)
    assert pipeline.lesson_due(learner, None, msk(14, 59), settings)
    assert not pipeline.lesson_due(learner, None, msk(15, 0), settings)


def test_lesson_is_not_due_twice_a_day_nor_retried_after_a_failure() -> None:
    settings, learner = make_settings(), learner_record()
    assert not pipeline.lesson_due(learner, "sent", msk(10), settings)
    assert not pipeline.lesson_due(learner, "failed", msk(10), settings)


def test_lesson_follows_the_learners_own_time_zone_and_send_time() -> None:
    learner = learner_record(timezone="Asia/Vladivostok", lesson_send_time=time(7, 30))
    # 07:30 in Vladivostok is 00:30 in Moscow.
    assert pipeline.lesson_due(learner, None, msk(0, 30, START + timedelta(days=1)), make_settings())
    assert not pipeline.lesson_due(learner, None, msk(9, 0), make_settings())


@pytest.mark.parametrize(
    "changes",
    [
        {"curriculum_started_on": None},
        {"curriculum_started_on": START + timedelta(days=1)},
        {"paused_at": datetime(2026, 10, 30, tzinfo=UTC)},
        {"is_active": False, "deactivated_reason": "blocked"},
    ],
)
def test_no_lesson_before_the_start_nor_while_paused_or_inactive(changes: dict[str, Any]) -> None:
    assert not pipeline.lesson_due(learner_record(**changes), None, msk(10), make_settings())


# --- the lesson tick ------------------------------------------------------------


def tick_settings() -> Settings:
    return make_settings(telegram_bot_token="123:TOKEN")


def started(learners: FakeLearners, chat_id: str = "42", **changes: Any) -> LearnerRecord:
    return learners.started(chat_id, START, **changes)


def test_tick_sends_and_records_once_per_day(
    lesson: Lesson, generated: list[str], fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)

    assert pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0)) == 1
    assert pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 1)) == 0  # e.g. the app restarted after the send

    assert len(generated) == 1
    assert len(sent) == 1
    record = lessons.rows[(learner.id, START)]
    assert (record.status, record.telegram_message_ids) == ("sent", [1])
    item = fake_items(stores).get_by_key(f"{generated[0]}:core")
    assert item is not None and item.source_lesson_id == record.id


def test_tick_serves_every_learner_on_their_own_schedule(
    generated: list[str], fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    stores, learners, _ = fake_stores()
    started(learners, "42")
    started(learners, "43", lesson_send_time=time(10, 0))

    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    pipeline.run_lesson_tick(tick_settings(), stores, msk(10, 0))
    assert [message["chat_id"] for message in sent] == ["42", "43"]


def test_tick_item_failure_does_not_fail_a_sent_lesson(
    generated: list[str], fake_telegram: None, caplog: pytest.LogCaptureFixture
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    fake_items(stores).failure = SQLAlchemyError("connection refused")

    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    assert lessons.rows[(learner.id, START)].status == "sent"
    assert "could not record the item" in caplog.text


def test_failed_day_keeps_its_topic_for_the_next_day(
    lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    asked: list[str] = []
    outcomes = iter([None, lesson])

    def fake_generate(topic: Any, settings: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson | None]:
        asked.append(topic.id)
        return research_result(topic.id), next(outcomes)

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)

    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    assert lessons.rows[(learner.id, START)].status == "failed"
    assert lessons.rows[(learner.id, START)].error == "boom"
    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 1))  # not retried every minute

    tomorrow = START + timedelta(days=1)
    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0, tomorrow))
    assert lessons.rows[(learner.id, tomorrow)].status == "sent"
    assert asked[0] == asked[1]  # the same topic
    assert len(sent) == 2  # the failure notice, then the lesson


def test_one_learners_failure_does_not_stop_the_others(
    lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: list[dict[str, Any]]
) -> None:
    stores, learners, _ = fake_stores()
    started(learners, "42", cefr="B1")
    started(learners, "43")

    monkeypatch.setattr(pipeline, "generate_lesson", lambda topic, *a, **k: (research_result(topic.id), lesson))
    monkeypatch.setattr(pipeline, "load_curriculum", unreadable_for_b1(pipeline.load_curriculum))
    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    assert [message["chat_id"] for message in sent] == ["43"]


def unreadable_for_b1(load: Any) -> Any:
    def load_curriculum(cefr: str = "A2") -> Any:
        if cefr == "B1":
            raise ValueError("broken curriculum file")
        return load(cefr)

    return load_curriculum


def test_finished_course_is_announced_once(fake_telegram: None, sent: list[dict[str, Any]]) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    mark_all_sent(lessons, learner, load_curriculum().topic_ids())

    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0, START + timedelta(days=1)))

    assert [message["text"] for message in sent] == [pipeline.COURSE_FINISHED]
    finished = learners.get("42")
    assert finished is not None and finished.deactivated_reason == "course_finished"


def mark_all_sent(lessons: FakeLessons, learner: LearnerRecord, topic_ids: list[str]) -> None:
    for offset, topic_id in enumerate(topic_ids, start=1):
        record = lessons.record_failed(
            learner.id, START - timedelta(days=offset), topic_id=topic_id, cefr="A2", reason="", curriculum_version=1
        )
        lessons.rows[(learner.id, record.lesson_date)] = record.model_copy(update={"status": "sent"})


def test_tick_teaches_the_learners_saved_level(
    lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None
) -> None:
    levels: list[str] = []

    def fake_generate(topic: Any, settings: Any, *, cefr: str, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        levels.append(cefr)
        return research_result(topic.id), lesson

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)
    stores, learners, _ = fake_stores()
    started(learners, cefr="B1")

    pipeline.run_lesson_tick(tick_settings(), stores, msk(9, 0))
    assert levels == ["B1"]


def test_main_no_send_does_not_open_the_database(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_open(settings: Settings) -> None:
        raise AssertionError("--no-send must not touch the database")

    monkeypatch.setattr(pipeline, "open_stores", must_not_open)
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", lambda *a, **k: (research_result(lesson.topic_id), lesson))
    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 0


def test_main_send_requires_database_url(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(telegram_bot_token="123:TOKEN", telegram_chat_id="42", database_url="")
    monkeypatch.setattr(pipeline, "open_stores", open_stores)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    assert pipeline.main(["--topic", "there_is_are"]) == 2
    assert "DATABASE_URL" in caplog.text
