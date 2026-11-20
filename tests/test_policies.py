from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from english_agent.day_plan import fallback_plan
from english_agent.models.curriculum import load_curriculum
from english_agent.models.plan import DayPlan, FallbackBranch, PracticeDirective, StepChoice
from english_agent.policies import MAX_DAY_MINUTES, PolicyContext, check_plan, dry_run
from english_agent.topic_graph import default_graph

TODAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)


def context(**changes: Any) -> PolicyContext:
    values: dict[str, Any] = {
        "today": TODAY,
        "graph": default_graph(),
        "known_topics": frozenset({"be_present"}),
        "studied_topics": frozenset({"be_present", "there_is_are"}),
        "due_reviews": 0,
        "ready_topics": frozenset({"be_present", "there_is_are", "have_got"}),
    }
    return PolicyContext(**{**values, **changes})


def plan(day_type: str = "new_topic", topic: str | None = "have_got", **changes: Any) -> DayPlan:
    values: dict[str, Any] = {
        "learner_id": 1,
        "plan_date": TODAY,
        "day_type": day_type,
        "step": StepChoice(topic_id=topic, step_index=1) if topic else None,
        "source": "agent",
        "evidence_ids": [1],
        "explanation_ru": "Сегодня новая тема, потому что прошлая освоена.",
        "created_at": NOW,
    }
    return DayPlan.model_validate({**values, **changes})


def rules(found: list[Any]) -> list[str]:
    return [violation.rule for violation in found]


def test_a_sound_plan_passes() -> None:
    assert check_plan(plan(), context()) == []


def test_a_new_topic_needs_its_prerequisites() -> None:
    found = check_plan(plan(topic="present_simple_third_person"), context())
    [violation] = [v for v in found if v.rule == "prerequisites"]
    assert "present_simple_habits" in violation.message


def test_due_reviews_cannot_be_cut_and_the_load_has_a_ceiling() -> None:
    small = plan(practice=PracticeDirective(quiz_size=1))
    assert rules(check_plan(small, context(due_reviews=4))) == ["reviews_cut"]
    big = plan(
        practice=PracticeDirective(quiz_size=12),
        step=StepChoice(topic_id="have_got", step_index=1, leaving_topic_id="be_present"),
    )
    assert "load" in rules(check_plan(big, context()))
    assert dry_run(big, context()).minutes > dry_run(plan(), context()).minutes
    assert dry_run(plan(), context()).minutes <= MAX_DAY_MINUTES


def test_exam_rules() -> None:
    exam = plan("exam", None, exam_topic_ids=["be_present", "there_is_are"])
    assert check_plan(exam, context()) == []
    assert rules(check_plan(exam, context(last_exam_on=TODAY - timedelta(days=3)))) == ["exam_gap"]
    assert rules(check_plan(exam, context(returned_on=TODAY - timedelta(days=1)))) == ["exam_after_pause"]
    unstudied = plan("exam", None, exam_topic_ids=["have_got"])
    assert rules(check_plan(unstudied, context())) == ["exam_topics"]


def test_an_agent_plan_cites_real_evidence_and_explains_itself() -> None:
    assert rules(check_plan(plan(evidence_ids=[]), context())) == ["evidence"]
    assert rules(check_plan(plan(), context(invalid_evidence=(7,)))) == ["evidence"]
    assert rules(check_plan(plan(explanation_ru=" "), context())) == ["explanation"]


def test_content_that_is_not_ready_needs_a_fallback_that_needs_none() -> None:
    unwritten = context(ready_topics=frozenset({"be_present"}))
    assert rules(check_plan(plan(), unwritten)) == ["fallback"]
    assert check_plan(plan(fallback=FallbackBranch(day_type="review_day")), unwritten) == []
    branch = FallbackBranch(day_type="continue_topic", step=StepChoice(topic_id="have_got", step_index=1))
    assert rules(check_plan(plan(fallback=branch), unwritten)) == ["fallback"]  # the branch needs content too


def test_revisiting_a_topic_needs_it_studied() -> None:
    assert rules(check_plan(plan("reteach", "have_got"), context())) == ["not_studied"]
    assert check_plan(plan("reteach", "there_is_are"), context()) == []


@pytest.mark.parametrize("known_count", [0, 1, 9, 30])
def test_the_fallback_policys_plans_pass_every_rule(known_count: int) -> None:
    topics = sorted(load_curriculum("A2").topics, key=lambda topic: topic.order)
    known = {topic.id for topic in topics[:known_count]}
    made = fallback_plan(
        learner_id=1,
        day=TODAY,
        cefr="A2",
        curriculum=load_curriculum("A2"),
        graph=default_graph(),
        progress={},
        skipped=set(),
        accuracy={},
        known=known,
        now=NOW,
    )
    assert made is not None
    ctx = context(
        known_topics=frozenset(known), studied_topics=frozenset(known), ready_topics=frozenset(), due_reviews=6
    )
    assert check_plan(made, ctx) == []
