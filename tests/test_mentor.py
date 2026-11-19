from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from english_agent.config import Settings
from english_agent.jobs.planner import mentor_due
from english_agent.llm import AssistantTurn, ToolCall, Usage
from english_agent.mentor import MentorOutcome, MentorRepos, run_mentor_session
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord
from english_agent.pipeline import Runtime
from english_agent.storage import (
    EvidenceRepo,
    KnowledgeRepo,
    LearnerRepo,
    MentorMemoryRepo,
    PlanRepo,
    SessionFactory,
    sql_stores,
)
from fakes import ScriptedLLM
from test_storage import SCHEDULER, _quiz_setup, wrong_option

DAY = date(2026, 11, 4)
NOW = datetime.now(UTC)  # the session reads "evidence of the last days" relative to now


def turn(*calls: tuple[str, dict[str, Any]], cost: float = 0.01) -> AssistantTurn:
    return AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id=f"c{n}", name=name, arguments=args) for n, (name, args) in enumerate(calls)],
        usage=Usage(cost_usd=cost),
    )


def review_day(evidence_ids: list[int]) -> dict[str, Any]:
    return {
        "plan": {
            "day_type": "review_day",
            "evidence_ids": evidence_ids,
            "explanation_ru": "Завтра только повторение: вчера было много ошибок в there is.",
        }
    }


async def setup(
    sessions: SessionFactory, lesson: Lesson, **settings: Any
) -> tuple[Runtime, MentorRepos, LearnerRecord]:
    practice, _, _ = await _quiz_setup(sessions, lesson)
    await practice.record_poll_answer("p0", "42", wrong_option(lesson, 0))
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    runtime = Runtime(
        Settings(_env_file=None, **{"mentor_mode": "live", **settings}),
        sql_stores(sessions, SCHEDULER),
        sender=cast(Any, None),  # a session never sends
    )
    repos = MentorRepos(
        memory=MentorMemoryRepo(sessions),
        knowledge=KnowledgeRepo(sessions, SCHEDULER),
        evidence=EvidenceRepo(sessions),
        plans=PlanRepo(sessions),
    )
    return runtime, repos, learner


async def evidence_ids(repos: MentorRepos, learner: LearnerRecord) -> list[int]:
    return [e.id for e in await repos.evidence.since(learner.id, datetime.min.replace(tzinfo=UTC))]


async def session(runtime: Runtime, repos: MentorRepos, learner: LearnerRecord, llm: ScriptedLLM) -> MentorOutcome:
    return await run_mentor_session(runtime, repos, learner, DAY, NOW, llm=llm)


async def test_the_mentor_reads_journals_and_commits_its_plan(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, learner = await setup(sessions, lesson)
    [cited] = await evidence_ids(repos, learner)
    note = {"decision": "День повторений.", "reason": "Ошибка в there is.", "evidence_ids": [cited]}
    llm = ScriptedLLM(
        [
            # One call a turn: the test's sessions share one connection (parallel reads are tested in test_agent_loop).
            turn(("learner_snapshot", {})),
            turn(("fresh_evidence", {})),
            turn(("journal_note", note)),
            turn(("commit_plan", review_day([cited]))),
        ]
    )

    assert await session(runtime, repos, learner, llm) == "committed"

    record = await runtime.stores.plans.get(learner.id, DAY)
    assert record is not None and record.state == "committed" and record.plan is not None
    assert (record.plan.source, record.plan.day_type, record.plan.evidence_until_id) == ("agent", "review_day", cited)
    assert record.trajectory is not None
    assert [entry["tool"] for entry in record.trajectory] == [
        "learner_snapshot",
        "fresh_evidence",
        "journal_note",
        "commit_plan",
    ]
    [entry] = await repos.memory.entries(learner.id)
    assert entry.evidence_ids == [cited]


async def test_a_refused_plan_comes_back_with_its_rule_and_is_fixed(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, learner = await setup(sessions, lesson)
    [cited] = await evidence_ids(repos, learner)
    exam = {
        "plan": {
            "day_type": "exam",
            "exam_topic_ids": ["have_got"],
            "evidence_ids": [cited],
            "explanation_ru": "Экзамен.",
        }
    }
    llm = ScriptedLLM([turn(("commit_plan", exam)), turn(("commit_plan", review_day([cited])))])

    assert await session(runtime, repos, learner, llm) == "committed"
    refusal = [m for m in llm.transcripts[1] if m["role"] == "tool"][-1]["content"]
    assert "exam_topics" in refusal and "have_got" in refusal


async def test_without_new_evidence_no_model_is_called(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, _ = await setup(sessions, lesson)
    quiet = await LearnerRepo(sessions).get_or_create("43", "A2")

    assert await session(runtime, repos, quiet, ScriptedLLM([])) == "no_new_evidence"
    record = await runtime.stores.plans.get(quiet.id, DAY)
    assert record is not None and record.state == "fallback" and record.plan is not None
    assert record.plan.source == "fallback"


async def test_running_out_of_steps_falls_back_and_keeps_the_trace(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, learner = await setup(sessions, lesson, mentor_max_steps=3)
    llm = ScriptedLLM([turn(("due_reviews", {}))] * 3)

    assert await session(runtime, repos, learner, llm) == "fallback"
    record = await runtime.stores.plans.get(learner.id, DAY)
    assert record is not None and record.state == "fallback" and record.trajectory is not None
    assert len(record.trajectory) == 3


async def test_the_budget_stops_a_session_and_the_fallback_plans(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, learner = await setup(sessions, lesson, mentor_max_calls=5, mentor_max_cost_usd=0.02)
    llm = ScriptedLLM([turn(("due_reviews", {}), cost=0.015)] * 5)

    assert await session(runtime, repos, learner, llm) == "fallback"
    record = await runtime.stores.plans.get(learner.id, DAY)
    assert record is not None and record.trajectory is not None
    assert "BudgetExceeded" in record.trajectory[-1]["error"]


async def test_in_shadow_mode_the_fallback_runs_and_the_mentors_plan_is_kept(
    sessions: SessionFactory, lesson: Lesson
) -> None:
    runtime, repos, learner = await setup(sessions, lesson, mentor_mode="shadow")
    [cited] = await evidence_ids(repos, learner)
    llm = ScriptedLLM([turn(("commit_plan", review_day([cited])))])

    assert await session(runtime, repos, learner, llm) == "shadow"
    record = await runtime.stores.plans.get(learner.id, DAY)
    assert record is not None and record.plan is not None and record.plan.source == "fallback"
    assert record.trajectory is not None and record.trajectory[-1]["shadow_plan"]["day_type"] == "review_day"


async def test_a_day_is_planned_once_and_off_means_off(sessions: SessionFactory, lesson: Lesson) -> None:
    runtime, repos, learner = await setup(sessions, lesson)
    [cited] = await evidence_ids(repos, learner)
    await session(runtime, repos, learner, ScriptedLLM([turn(("commit_plan", review_day([cited])))]))
    assert await session(runtime, repos, learner, ScriptedLLM([])) == "already_planned"

    off = Runtime(Settings(_env_file=None), runtime.stores, sender=cast(Any, None))
    assert await run_mentor_session(off, repos, learner, date(2026, 11, 5), NOW, llm=ScriptedLLM([])) == "off"


@pytest.mark.parametrize(
    ("hour", "minute", "due"), [(20, 59, False), (21, 0, True), (21, 9, True), (21, 10, False), (9, 0, False)]
)
def test_the_evening_window(hour: int, minute: int, due: bool) -> None:
    from datetime import time

    local = datetime(2026, 11, 3, hour, minute, tzinfo=UTC)
    assert mentor_due(local, time(21, 0)) is due
