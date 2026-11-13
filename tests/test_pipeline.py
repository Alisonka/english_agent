from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import SendMessage
from sqlalchemy.exc import SQLAlchemyError

from english_agent import pipeline
from english_agent.config import Settings
from english_agent.content import content_key
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord
from english_agent.research.models import ResearchResult
from english_agent.storage import Stores, open_stores
from english_agent.telegram import Sender, make_bot
from english_agent.telegram import sender as sender_module
from fakes import FakeLearners, FakeLessons, fake_contents, fake_items, fake_stores
from telegram_fake import FakeTelegram, blocked_by, fake_sender


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def stores(monkeypatch: pytest.MonkeyPatch) -> Stores:
    """Every `main()` run records into in-memory repositories instead of PostgreSQL."""
    fakes, _, _ = fake_stores()

    @asynccontextmanager
    async def open_fakes(settings: Settings) -> AsyncIterator[Stores]:
        yield fakes

    monkeypatch.setattr(pipeline, "open_stores", open_fakes)
    return fakes


@pytest.fixture
def telegram_settings() -> Settings:
    return make_settings(telegram_bot_token="123:TOKEN", telegram_chat_id="42")


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


class Sent:
    """The messages sent so far, as {"chat_id", "text"} dicts."""

    def __init__(self, telegram: FakeTelegram) -> None:
        self._telegram = telegram

    def _all(self) -> list[dict[str, Any]]:
        delivered = [m for m in self._telegram.delivered if isinstance(m, SendMessage)]
        return [{"chat_id": str(m.chat_id), "text": m.text} for m in delivered]

    def __len__(self) -> int:
        return len(self._all())

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self._all()[index]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._all())

    def __eq__(self, other: object) -> bool:
        return self._all() == other


@pytest.fixture
def sent(telegram: FakeTelegram) -> Sent:
    return Sent(telegram)


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch, telegram: FakeTelegram) -> None:
    """Command-line runs send through the fake Bot API."""

    @asynccontextmanager
    async def open_fake(settings: Settings) -> AsyncIterator[Sender]:
        yield fake_sender(telegram)

    monkeypatch.setattr(pipeline, "open_sender", open_fake)


Tick = Callable[..., Awaitable[int]]


@pytest.fixture
def run_tick(telegram: FakeTelegram) -> Tick:
    """`run_lesson_tick` with the fake Bot API: run_tick(stores, now, settings=tick_settings())."""

    async def run(stores: Stores, now: datetime, settings: Settings | None = None) -> int:
        runtime = pipeline.Runtime(settings or tick_settings(), stores, fake_sender(telegram))
        return await pipeline.run_lesson_tick(runtime, now)

    return run


def research_result(topic_id: str, status: str = "failed", message: str = "boom") -> ResearchResult:
    """A research outcome without notes; `generate_lesson` is stubbed, so the lesson comes separately."""
    return ResearchResult.model_validate(
        {"topic_id": topic_id, "status": status, "message": message, "sites_checked": [], "steps": 0, "trajectory": []}
    )


def returns(value: Any) -> Any:
    """An async stand-in for `generate_lesson` that always returns `value`."""

    async def stub(*args: Any, **kwargs: Any) -> Any:
        return value

    return stub


def echo_topic(lesson: Lesson) -> Any:
    """An async stand-in for `generate_lesson` that writes `lesson` for whatever topic it is asked."""

    async def stub(topic: Any, *args: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        return research_result(topic.id), lesson

    return stub


async def test_deliver_lesson_sends_to_the_chat(lesson: Lesson, telegram: FakeTelegram, sent: Sent) -> None:
    assert await pipeline.deliver_lesson(fake_sender(telegram), lesson, "42") == [1]
    assert sent[0]["chat_id"] == "42"
    assert lesson.title_en in sent[0]["text"]


def test_a_bot_needs_a_token() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        make_bot(make_settings())


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

    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("generate_lesson must not be called")

    monkeypatch.setattr(pipeline, "generate_lesson", must_not_run)
    assert pipeline.main(["--topic", "there_is_are"]) == 2
    assert "--no-send" in caplog.text


def test_main_no_send_prints_lesson_without_telegram(
    lesson: Lesson,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_telegram: None,
    sent: Sent,
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", returns((research_result(lesson.topic_id), lesson)))

    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 0
    assert json.loads(capsys.readouterr().out)["topic_id"] == "there_is_are"
    assert sent == []


def test_main_sends_lesson_and_reports(
    lesson: Lesson,
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_telegram: None,
    sent: Sent,
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", returns((research_result(lesson.topic_id), lesson)))

    assert pipeline.main(["--topic", "there_is_are"]) == 0
    assert "sent 1 message(s) to Telegram chat 42" in caplog.text
    assert len(sent) == 1


def test_main_reports_telegram_failure_and_stops_writing_to_a_blocked_chat(
    lesson: Lesson,
    telegram_settings: Settings,
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    telegram: FakeTelegram,
    fake_telegram: None,
) -> None:
    telegram.fail = blocked_by("42")
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", returns((research_result(lesson.topic_id), lesson)))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "bot was blocked" in caplog.text
    learner = cast(FakeLearners, stores.learners).by_chat.get("42")
    assert learner is not None and learner.deactivated_reason == "blocked"


def test_main_when_research_finds_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", returns((result, None)))

    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 1
    assert "нет на сайте" in caplog.text


def test_main_tells_the_learner_when_research_finds_nothing(
    telegram_settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: Sent
) -> None:
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    result = research_result("there_is_are", status="not_found", message="нет на сайте")
    monkeypatch.setattr(pipeline, "generate_lesson", returns((result, None)))

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    [notice] = sent
    assert "не получился" in notice["text"] and "завтра" in notice["text"]


def test_main_keeps_error_details_out_of_the_chat(
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_telegram: None,
    sent: Sent,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("API <down> & out")

    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", boom)

    assert pipeline.main(["--topic", "there_is_are"]) == 1
    assert "RuntimeError: API <down> & out" in caplog.text
    assert "API" not in sent[0]["text"]


def test_main_failure_notice_that_cannot_be_delivered_is_logged(
    telegram_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    telegram: FakeTelegram,
    fake_telegram: None,
) -> None:
    telegram.fail = blocked_by("42")
    monkeypatch.setattr(pipeline, "get_settings", lambda: telegram_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", returns((research_result("there_is_are"), None)))

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

    async def fake_generate(topic: Any, settings: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
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
    sent: Sent,
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
    sent: Sent,
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


MSK = ZoneInfo("Europe/Moscow")


def msk(hour: int, minute: int = 0, day: date = START) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=MSK)


# --- the lesson tick ------------------------------------------------------------


def tick_settings() -> Settings:
    return make_settings(telegram_bot_token="123:TOKEN")


def started(learners: FakeLearners, chat_id: str = "42", **changes: Any) -> LearnerRecord:
    return learners.started(chat_id, START, **changes)


async def test_tick_sends_and_records_once_per_day(
    run_tick: Tick, lesson: Lesson, generated: list[str], fake_telegram: None, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)

    assert await run_tick(stores, msk(9, 0)) == 1
    assert await run_tick(stores, msk(9, 1)) == 0  # e.g. the app restarted after the send

    assert len(generated) == 1
    assert len(sent) == 1
    record = lessons.rows[(learner.id, START)]
    assert (record.status, record.telegram_message_ids) == ("sent", [1])
    item = fake_items(stores).get_by_key(f"{generated[0]}:core")
    assert item is not None and item.source_lesson_id == record.id


async def test_tick_serves_every_learner_on_their_own_schedule(
    run_tick: Tick, generated: list[str], fake_telegram: None, sent: Sent
) -> None:
    stores, learners, _ = fake_stores()
    started(learners, "42")
    started(learners, "43", lesson_send_time=time(10, 0))

    await run_tick(stores, msk(9, 0))
    await run_tick(stores, msk(10, 0))
    assert [message["chat_id"] for message in sent] == ["42", "43"]


async def test_tick_item_failure_does_not_fail_a_sent_lesson(
    run_tick: Tick, generated: list[str], fake_telegram: None, caplog: pytest.LogCaptureFixture
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    fake_items(stores).failure = SQLAlchemyError("connection refused")

    await run_tick(stores, msk(9, 0))
    assert lessons.rows[(learner.id, START)].status == "sent"
    assert "could not record the item" in caplog.text


async def test_failed_day_keeps_its_topic_for_the_next_day(
    run_tick: Tick, lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    asked: list[str] = []
    outcomes = iter([None, lesson])

    async def fake_generate(topic: Any, settings: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson | None]:
        asked.append(topic.id)
        return research_result(topic.id), next(outcomes)

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)

    await run_tick(stores, msk(9, 0))
    assert lessons.rows[(learner.id, START)].status == "skipped"  # the topic comes again tomorrow
    assert lessons.rows[(learner.id, START)].error == "boom"
    await run_tick(stores, msk(9, 1))  # not retried every minute

    tomorrow = START + timedelta(days=1)
    await run_tick(stores, msk(9, 0, tomorrow))
    assert lessons.rows[(learner.id, tomorrow)].status == "sent"
    assert asked[0] == asked[1]  # the same topic
    assert len(sent) == 2  # the failure notice, then the lesson


async def test_one_learners_failure_does_not_stop_the_others(
    run_tick: Tick, lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None, sent: Sent
) -> None:
    stores, learners, _ = fake_stores()
    started(learners, "42", cefr="B1")
    started(learners, "43")

    monkeypatch.setattr(pipeline, "generate_lesson", echo_topic(lesson))
    monkeypatch.setattr(pipeline, "load_curriculum", unreadable_for_b1(pipeline.load_curriculum))
    await run_tick(stores, msk(9, 0))
    assert [message["chat_id"] for message in sent] == ["43"]


def unreadable_for_b1(load: Any) -> Any:
    def load_curriculum(cefr: str = "A2") -> Any:
        if cefr == "B1":
            raise ValueError("broken curriculum file")
        return load(cefr)

    return load_curriculum


async def test_finished_course_is_announced_once(run_tick: Tick, fake_telegram: None, sent: Sent) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    await mark_all_sent(lessons, learner, load_curriculum().topic_ids())

    await run_tick(stores, msk(9, 0))
    await run_tick(stores, msk(9, 0, START + timedelta(days=1)))

    assert [message["text"] for message in sent] == [pipeline.COURSE_FINISHED]
    finished = learners.by_chat.get("42")
    assert finished is not None and finished.deactivated_reason == "course_finished"


async def mark_all_sent(lessons: FakeLessons, learner: LearnerRecord, topic_ids: list[str]) -> None:
    for offset, topic_id in enumerate(topic_ids, start=1):
        record = await lessons.record_failed(
            learner.id, START - timedelta(days=offset), topic_id=topic_id, cefr="A2", reason="", curriculum_version=1
        )
        lessons.rows[(learner.id, record.lesson_date)] = record.model_copy(update={"status": "sent"})


async def test_tick_teaches_the_learners_saved_level(
    run_tick: Tick, lesson: Lesson, monkeypatch: pytest.MonkeyPatch, fake_telegram: None
) -> None:
    levels: list[str] = []

    async def fake_generate(topic: Any, settings: Any, *, cefr: str, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        levels.append(cefr)
        return research_result(topic.id), lesson

    monkeypatch.setattr(pipeline, "generate_lesson", fake_generate)
    stores, learners, _ = fake_stores()
    started(learners, cefr="B1")

    await run_tick(stores, msk(9, 0))
    assert levels == ["B1"]


def test_main_no_send_does_not_open_the_database(lesson: Lesson, monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_open(settings: Settings) -> None:
        raise AssertionError("--no-send must not touch the database")

    monkeypatch.setattr(pipeline, "open_stores", must_not_open)
    monkeypatch.setattr(pipeline, "get_settings", make_settings)
    monkeypatch.setattr(pipeline, "generate_lesson", returns((research_result(lesson.topic_id), lesson)))
    assert pipeline.main(["--topic", "there_is_are", "--no-send"]) == 0


def test_main_send_requires_database_url(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(telegram_bot_token="123:TOKEN", telegram_chat_id="42", database_url="")
    monkeypatch.setattr(pipeline, "open_stores", open_stores)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    assert pipeline.main(["--topic", "there_is_are"]) == 2
    assert "DATABASE_URL" in caplog.text


# --- shared lesson content -------------------------------------------------------


async def test_learners_on_the_same_topic_share_one_written_lesson(
    run_tick: Tick, generated: list[str], fake_telegram: None, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    first, second = started(learners, "42"), started(learners, "43", lesson_send_time=time(11, 0))

    await run_tick(stores, msk(9, 0))
    await run_tick(stores, msk(11, 0))

    assert len(generated) == 1  # research and writing ran once
    assert [message["chat_id"] for message in sent] == ["42", "43"]
    content_ids = {lessons.rows[(learner.id, START)].content_id for learner in (first, second)}
    assert len(content_ids) == 1 and None not in content_ids


async def test_content_being_written_elsewhere_is_retried_on_the_next_tick(
    run_tick: Tick, lesson: Lesson, generated: list[str], fake_telegram: None, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    topic = load_curriculum().topic_ids()[0]
    settings = tick_settings()
    version = load_curriculum().version
    outcome, record = await fake_contents(stores).claim(
        topic,
        "A2",
        version,
        content_key(settings),
        owner="other",
        now=msk(8, 59),
        stale_after=timedelta(minutes=20),
        max_attempts=5,
    )
    assert outcome == "claimed"

    assert await run_tick(stores, msk(9, 0), settings) == 0
    released = lessons.rows[(learner.id, START)]
    assert (released.status, released.claimed_at, sent) == ("pending", None, [])  # let go: retried next tick

    await fake_contents(stores).finish(record.id, lesson)
    assert await run_tick(stores, msk(9, 1), settings) == 1
    assert generated == [] and len(sent) == 1


async def test_a_topic_whose_every_attempt_failed_is_skipped(
    run_tick: Tick, generated: list[str], fake_telegram: None, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    ordered = load_curriculum().topic_ids()
    settings = make_settings(telegram_bot_token="123:TOKEN", job_max_attempts=1)
    version = load_curriculum().version
    _, record = await fake_contents(stores).claim(
        ordered[0],
        "A2",
        version,
        content_key(settings),
        owner="other",
        now=msk(7),
        stale_after=timedelta(minutes=20),
        max_attempts=1,
    )
    await fake_contents(stores).fail(record.id, "нет на сайте")

    await run_tick(stores, msk(9, 0), settings)
    assert generated == [ordered[1]]
    assert lessons.rows[(learner.id, START)].topic_id == ordered[1]


# --- interrupted deliveries ----------------------------------------------------------


def three_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every lesson renders as three messages, so a send can be cut short in the middle."""
    monkeypatch.setattr(sender_module, "render_messages", lambda lesson: ["part 1", "part 2", "part 3"])


async def test_a_send_cut_short_by_telegram_is_retried_after_a_backoff_without_repeats(
    lesson: Lesson, monkeypatch: pytest.MonkeyPatch, telegram: FakeTelegram, run_tick: Tick, sent: Sent
) -> None:
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    three_messages(monkeypatch)
    monkeypatch.setattr(pipeline, "generate_lesson", echo_topic(lesson))
    outage = {"calls": 0}

    def second_message_fails(method: Any) -> tuple[int, dict[str, Any]] | None:
        if isinstance(method, SendMessage):
            outage["calls"] += 1
            if outage["calls"] == 2:
                return 502, {"error_code": 502, "description": "Bad Gateway"}
        return None

    telegram.fail = second_message_fails
    await run_tick(stores, msk(9, 0))
    row = lessons.rows[(learner.id, START)]
    assert (row.status, len(row.telegram_message_ids)) == ("failed", 1)

    await run_tick(stores, msk(9, 1))  # inside the backoff: nothing happens
    assert lessons.rows[(learner.id, START)].status == "failed"

    await run_tick(stores, msk(9, 10))
    row = lessons.rows[(learner.id, START)]
    assert row.status == "sent"
    texts = [message["text"] for message in sent]
    assert len(texts) == len(set(texts)) == len(row.telegram_message_ids)  # every chunk exactly once


async def test_a_worker_that_died_mid_send_is_taken_over_and_resumed(
    lesson: Lesson, monkeypatch: pytest.MonkeyPatch, run_tick: Tick, sent: Sent
) -> None:
    three_messages(monkeypatch)
    stores, learners, lessons = fake_stores()
    learner = started(learners)
    version = load_curriculum().version
    _, row = await lessons.claim(
        learner.id,
        START,
        topic_id=lesson.topic_id,
        cefr="A2",
        curriculum_version=version,
        now=msk(9, 0),
        stale_after=timedelta(minutes=10),
        max_attempts=5,
    )
    await lessons.start_sending(row.id, 1, lesson)
    await lessons.add_message_id(row.id, 77)  # the first chunk went out, then the worker died

    await run_tick(stores, msk(9, 5))  # the claim is still fresh: nobody takes over
    assert sent == []
    await run_tick(stores, msk(9, 11))
    done = lessons.rows[(learner.id, START)]
    assert done.status == "sent" and done.telegram_message_ids[0] == 77
    assert [message["text"] for message in sent] == ["part 2", "part 3"]  # only what had not arrived
