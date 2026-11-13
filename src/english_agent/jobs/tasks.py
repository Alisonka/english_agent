"""The jobs. Each one is thin: it binds log fields, calls a service, and clears its de-duplication key.

Every job is safe to run twice: lessons and content are claimed in PostgreSQL, practice blocks
are claimed per lesson and kind, a translation is graded only while pending, profiles are
append-only with a change check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

from taskiq import TaskiqDepends

from english_agent.content import content_key
from english_agent.grading import give_up_grading, grade_answer, refresh_profile_safely
from english_agent.jobs.broker import broker
from english_agent.jobs.dedup import (
    content_key_for,
    enqueue,
    grading_key,
    lesson_key,
    practice_key,
    profile_key,
)
from english_agent.jobs.deps import WorkerDeps, worker_deps
from english_agent.jobs.planner import plan_tick
from english_agent.logs import bind
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import load_curriculum
from english_agent.pipeline import ensure_content, next_topic, run_lesson_for_learner
from english_agent.practice import deliver_practice_to

log = logging.getLogger(__name__)

# How long a de-duplication key outlives a worker that died holding it.
LESSON_KEY_TTL = 30 * 60
PRACTICE_KEY_TTL = 10 * 60
GRADING_KEY_TTL = 10 * 60
PROFILE_KEY_TTL = 60
CONTENT_KEY_TTL = 40 * 60
LESSON_TIMEOUT_SECONDS = 25 * 60  # research + writing + sending; past this the claim is taken over


@asynccontextmanager
async def _job(deps: WorkerDeps, key: str, **fields: object) -> AsyncIterator[None]:
    """Log fields for the job's lines; the key is cleared whatever happens, so the next tick may retry."""
    try:
        with bind(job_key=key, **fields):
            yield
    finally:
        await deps.dedup.release(key)


@broker.task(task_name="schedule_tick", schedule=[{"cron": "* * * * *"}])
async def schedule_tick(deps: WorkerDeps = TaskiqDepends(worker_deps)) -> None:
    """Every minute: enqueue what is due. Enqueueing is de-duplicated, so overlapping ticks are harmless."""
    now = datetime.now(UTC)
    runtime = deps.runtime
    plan = await plan_tick(runtime, now)
    sent = 0
    for learner_id, day in plan.lessons:
        sent += await enqueue(
            deps.dedup, lesson_key(learner_id, day), LESSON_KEY_TTL, deliver_lesson, learner_id, day.isoformat()
        )
    for learner_id, day in plan.practice:
        sent += await enqueue(
            deps.dedup, practice_key(learner_id, day), PRACTICE_KEY_TTL, deliver_practice, learner_id, day.isoformat()
        )
    for answer_id in plan.gradings:
        sent += await enqueue(deps.dedup, grading_key(answer_id), GRADING_KEY_TTL, grade_translation, answer_id)
    for answer_id in plan.abandoned_gradings:
        log.warning("grading of answer %s gave up after its attempts", answer_id)
        await give_up_grading(runtime.stores, runtime.sender, answer_id, "grading did not finish")
    closed = await runtime.stores.lessons.close_past((now - timedelta(days=2)).date())
    if sent or closed or plan.abandoned_gradings:
        log.info("tick: %d job(s) enqueued, %d past day(s) closed", sent, closed)


@broker.task(task_name="deliver_lesson")
async def deliver_lesson(learner_id: int, day: str, deps: WorkerDeps = TaskiqDepends(worker_deps)) -> int:
    lesson_day = date.fromisoformat(day)
    async with _job(deps, lesson_key(learner_id, lesson_day), learner_id=learner_id):
        learner = await deps.runtime.stores.learners.get_by_id(learner_id)
        if learner is None or not learner.receives_lessons:
            return 0
        with bind(chat_id=learner.telegram_chat_id):
            async with asyncio.timeout(LESSON_TIMEOUT_SECONDS):
                return await run_lesson_for_learner(deps.runtime, learner, lesson_day)


@broker.task(task_name="deliver_practice")
async def deliver_practice(learner_id: int, day: str, deps: WorkerDeps = TaskiqDepends(worker_deps)) -> str | None:
    lesson_day = date.fromisoformat(day)
    async with _job(deps, practice_key(learner_id, lesson_day), learner_id=learner_id):
        runtime = deps.runtime
        learner = await runtime.stores.learners.get_by_id(learner_id)
        if learner is None or not learner.receives_lessons:
            return None
        return await deliver_practice_to(
            runtime.sender, runtime.settings, runtime.stores, learner, lesson_day, datetime.now(UTC)
        )


@broker.task(task_name="grade_translation", retry_on_error=True)
async def grade_translation(answer_id: int, deps: WorkerDeps = TaskiqDepends(worker_deps)) -> bool:
    async with _job(deps, grading_key(answer_id), answer_id=answer_id):
        return await grade_answer(deps.runtime.stores, deps.runtime.sender, deps.grade, answer_id, retry_transient=True)


@broker.task(task_name="refresh_profile", retry_on_error=True)
async def refresh_profile(learner_id: int, deps: WorkerDeps = TaskiqDepends(worker_deps)) -> None:
    async with _job(deps, profile_key(learner_id), learner_id=learner_id):
        await refresh_profile_safely(deps.runtime.stores.profiles, learner_id)


@broker.task(task_name="prewarm_content", schedule=[{"cron": "17 */6 * * *"}])
async def prewarm_content(deps: WorkerDeps = TaskiqDepends(worker_deps)) -> int:
    """Write ahead the next lesson of every active learner, so their send time never waits for the model."""
    runtime = deps.runtime
    settings, stores = runtime.settings, runtime.stores
    wanted: set[tuple[str, Cefr]] = set()
    recipe = content_key(settings)
    for learner in await stores.learners.list_active():
        curriculum = load_curriculum(learner.cefr)
        done = await stores.lessons.sent_topic_ids(learner.id, learner.cefr)
        skipped = await stores.contents.unavailable_topics(
            learner.cefr, curriculum.version, recipe, max_attempts=settings.job_max_attempts
        )
        topic = next_topic(curriculum, done | skipped)
        if topic is not None:
            wanted.add((topic.id, learner.cefr))
    queued = 0
    for topic_id, cefr in sorted(wanted):
        queued += await enqueue(
            deps.dedup, content_key_for(topic_id, cefr), CONTENT_KEY_TTL, generate_content, topic_id, cefr
        )
    log.info("prewarm: %d topic(s) wanted, %d job(s) enqueued", len(wanted), queued)
    return queued


@broker.task(task_name="generate_content", retry_on_error=True)
async def generate_content(topic_id: str, cefr: Cefr, deps: WorkerDeps = TaskiqDepends(worker_deps)) -> str | None:
    async with _job(deps, content_key_for(topic_id, cefr), topic_id=topic_id, cefr=cefr):
        curriculum = load_curriculum(cefr)
        topic = next((item for item in curriculum.topics if item.id == topic_id), None)
        if topic is None:
            return None
        record = await ensure_content(deps.runtime, topic, cefr, curriculum.version)
        return None if record is None else record.status
