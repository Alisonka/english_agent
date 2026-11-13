"""Jobs without a broker: the tick's plan, de-duplication, and each job body over in-memory stores."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conftest import lesson_payload
from english_agent.config import Settings
from english_agent.jobs import tasks
from english_agent.jobs.dedup import LocalDeduper, enqueue, lesson_key
from english_agent.jobs.deps import WorkerDeps
from english_agent.jobs.planner import plan_tick
from english_agent.llm import LLMUnavailable
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.pipeline import Runtime
from english_agent.research.models import ResearchResult
from english_agent.storage import Stores
from fakes import FakeLearners, FakeLessons, fake_practice, fake_stores
from telegram_fake import FakeTelegram, fake_sender

DAY = date(2026, 11, 6)
MSK = ZoneInfo("Europe/Moscow")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=MSK)


def runtime(stores: Stores, telegram: FakeTelegram | None = None) -> Runtime:
    settings = Settings(_env_file=None, telegram_bot_token="123:TOKEN")
    return Runtime(settings, stores, fake_sender(telegram or FakeTelegram()))


async def never_grade(lesson: Lesson, text: str) -> TranslationGrade:
    raise AssertionError("not expected to grade")


def deps(stores: Stores, telegram: FakeTelegram | None = None, grade: Any = never_grade) -> WorkerDeps:
    return WorkerDeps(runtime(stores, telegram), LocalDeduper(), grade)


# --- the tick's plan ---------------------------------------------------------------------------


async def sent_lesson(learners: FakeLearners, lessons: FakeLessons, chat_id: str, sent_at: datetime) -> int:
    student = learners.started(chat_id, DAY)
    record = await lessons.record_sent(
        student.id, DAY, Lesson.model_validate(lesson_payload()), [1], curriculum_version=1
    )
    lessons.rows[(student.id, DAY)] = record.model_copy(update={"sent_at": sent_at})
    return student.id


async def test_the_plan_has_due_lessons_and_practice_and_stale_gradings() -> None:
    stores, learners, lessons = fake_stores()
    waiting = learners.started("1", DAY).id  # 09:00 lesson, nothing sent yet
    learners.started("2", DAY, lesson_send_time=time(11, 0))  # not yet
    quiz_due = await sent_lesson(learners, lessons, "3", at(8, 0))  # sent an hour ago: the quiz is due
    practice = fake_practice(stores)
    task = await practice.claim_delivery(1, "translation", 3)
    assert task is not None
    stale = await practice.start_translation_answer(task.id, quiz_due, "answer")
    assert stale is not None
    practice.translation_answers[stale.id] = stale.model_copy(update={"created_at": at(8, 0)})

    plan = await plan_tick(runtime(stores), at(9, 30))

    assert plan.lessons == [(waiting, DAY)]
    assert plan.practice == [(quiz_due, DAY)]
    assert (plan.gradings, plan.abandoned_gradings) == ([stale.id], [])


async def test_a_grading_out_of_attempts_is_abandoned_not_retried() -> None:
    stores, learners, lessons = fake_stores()
    student = await sent_lesson(learners, lessons, "3", at(8, 0))
    practice = fake_practice(stores)
    task = await practice.claim_delivery(1, "translation", 3)
    assert task is not None
    answer = await practice.start_translation_answer(task.id, student, "answer")
    assert answer is not None
    practice.translation_answers[answer.id] = answer.model_copy(
        update={"grading_attempts": 3, "grading_started_at": at(8, 0)}
    )

    plan = await plan_tick(runtime(stores), at(9, 30))
    assert (plan.gradings, plan.abandoned_gradings) == ([], [answer.id])


# --- de-duplication -----------------------------------------------------------------------


class RecordingTask:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[Any, ...]] = []
        self.fail = fail

    async def kiq(self, *args: Any) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.sent.append(args)


async def test_the_same_work_is_enqueued_once_until_its_job_lets_go() -> None:
    dedup, task = LocalDeduper(), RecordingTask()
    assert await enqueue(dedup, "lesson:1", 60, task, 1)  # type: ignore[arg-type]
    assert not await enqueue(dedup, "lesson:1", 60, task, 1)  # type: ignore[arg-type]
    await dedup.release("lesson:1")
    assert await enqueue(dedup, "lesson:1", 60, task, 1)  # type: ignore[arg-type]
    assert task.sent == [(1,), (1,)]


async def test_a_failed_send_frees_the_key_for_the_next_tick() -> None:
    dedup = LocalDeduper()
    with pytest.raises(ConnectionError):
        await enqueue(dedup, "lesson:1", 60, RecordingTask(fail=True), 1)  # type: ignore[arg-type]
    assert await dedup.claim("lesson:1", 60)


# --- job bodies ---------------------------------------------------------------------------


def research(topic_id: str) -> ResearchResult:
    return ResearchResult.model_validate(
        {"topic_id": topic_id, "status": "found", "message": "ok", "sites_checked": [], "steps": 0, "trajectory": []}
    )


async def test_deliver_lesson_sends_and_clears_its_key(monkeypatch: pytest.MonkeyPatch, lesson: Lesson) -> None:
    from english_agent import pipeline

    async def generate(topic: Any, *args: Any, **kwargs: Any) -> tuple[ResearchResult, Lesson]:
        return research(topic.id), lesson.model_copy(update={"topic_id": topic.id})

    monkeypatch.setattr(pipeline, "generate_lesson", generate)
    stores, learners, lessons = fake_stores()
    student = learners.started("42", date(2026, 11, 1))
    telegram = FakeTelegram()
    worker = deps(stores, telegram)
    today = datetime.now(UTC).astimezone(MSK).date()
    await worker.dedup.claim(lesson_key(student.id, today), 60)  # what the tick did

    await tasks.deliver_lesson.original_func(student.id, today.isoformat(), deps=worker)

    assert lessons.rows[(student.id, today)].status == "sent"
    assert telegram.delivered
    assert await worker.dedup.claim(lesson_key(student.id, today), 60)  # released


async def test_deliver_lesson_skips_a_learner_who_paused_meanwhile() -> None:
    stores, learners, lessons = fake_stores()
    student = learners.started("42", date(2026, 11, 1), paused_at=datetime(2026, 11, 2, tzinfo=UTC))
    await tasks.deliver_lesson.original_func(student.id, DAY.isoformat(), deps=deps(stores))
    assert lessons.rows == {}


async def grading_setup() -> tuple[Stores, int]:
    stores, learners, lessons = fake_stores()
    student = await sent_lesson(learners, lessons, "42", at(8, 0))
    practice = fake_practice(stores)
    task = await practice.claim_delivery(1, "translation", 3)
    assert task is not None
    await practice.mark_sent(task.id, [5])
    answer = await practice.start_translation_answer(task.id, student, "There is a window in my room.")
    assert answer is not None
    return stores, answer.id


async def test_a_grading_job_keeps_the_answer_pending_while_the_model_is_down() -> None:
    async def down(lesson: Lesson, text: str) -> TranslationGrade:
        raise LLMUnavailable("HTTP 529 OverloadedError")

    stores, answer_id = await grading_setup()
    worker = deps(stores, grade=down)
    for _ in range(2):
        with pytest.raises(LLMUnavailable):  # the job is retried
            await tasks.grade_translation.original_func(answer_id, deps=worker)
    answer = fake_practice(stores).translation_answers[answer_id]
    assert (answer.status, answer.grading_attempts) == ("pending", 2)

    await tasks.grade_translation.original_func(answer_id, deps=worker)  # the last attempt gives up
    assert fake_practice(stores).translation_answers[answer_id].status == "failed"


async def test_the_tick_gives_up_abandoned_gradings_and_closes_past_days(monkeypatch: pytest.MonkeyPatch) -> None:
    stores, answer_id = await grading_setup()
    practice = fake_practice(stores)
    practice.translation_answers[answer_id] = practice.translation_answers[answer_id].model_copy(
        update={"grading_attempts": 3, "created_at": at(0, 0)}
    )
    enqueued: list[tuple[Any, ...]] = []

    async def record(dedup: Any, key: str, ttl: int, task: Any, *args: Any) -> bool:
        enqueued.append((task.task_name, *args))
        return True

    monkeypatch.setattr(tasks, "enqueue", record)
    telegram = FakeTelegram()
    await tasks.schedule_tick.original_func(deps=deps(stores, telegram))

    assert practice.translation_answers[answer_id].status == "failed"
    assert any("Не получилось проверить" in text for text in telegram.texts)
    assert all(name != "grade_translation" for name, *_ in enqueued)
