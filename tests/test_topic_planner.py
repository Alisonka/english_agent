from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conftest import topic_content_payload
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.llm.forced_tool import LLMWriteError
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.topic import TopicPlan
from english_agent.research.models import TopicNotes
from english_agent.topic_planner import plan_problems, plan_topic, step_budget
from fakes import ScriptedLLM

TOPIC = CurriculumTopic(
    id="there_is_are", title_en="there is / there are", title_ru="there is / there are", order=13, tags=["there"]
)

NOTES = TopicNotes(
    site="skysmart",
    source={"url": "https://skysmart.ru/x", "title": "There is / are", "accessed_at": "2026-10-16"},
    page_title="There is / are",
    summary_ru="Статья про there is / there are, как сказать, что где-то что-то есть.",
    rule_points=["there is + ед.", "there is неисч.", "there are + мн.", "вопрос Is/Are there"],
    nuances=["согласование с первым", "there не значит там", "короткие ответы"],
    examples=[
        {"en": "There is a cat here.", "ru": "Здесь кот."},
        {"en": "There is some milk.", "ru": "Есть молоко."},
        {"en": "There are two books.", "ru": "Две книги."},
        {"en": "Is there a bank here?", "ru": "Здесь есть банк?"},
    ],
    typical_mistakes=["It is a cat → There is a cat"],
)


def _plan_args() -> dict[str, Any]:
    plan = topic_content_payload()["plan"]
    return {"steps": plan["steps"], "characters": plan["characters"]}


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="c1", name="submit_plan", arguments=args)])


@pytest.mark.parametrize(
    ("rules", "nuances", "expected"),
    [(1, 0, 1), (4, 0, 1), (5, 0, 2), (8, 0, 2), (9, 0, 3), (20, 20, 4)],
)
def test_step_budget(rules: int, nuances: int, expected: int) -> None:
    notes = NOTES.model_copy(update={"rule_points": ["x"] * rules, "nuances": ["y"] * nuances})
    assert step_budget(notes) == expected


async def test_plan_topic_fills_code_owned_fields() -> None:
    llm = ScriptedLLM([submit({**_plan_args(), "topic_id": "wrong", "cefr": "C1"})])
    plan = await plan_topic(llm, TOPIC, NOTES)
    assert plan.topic_id == "there_is_are" and plan.cefr == "A2"
    assert plan.slugs == ["singular", "plural"]
    assert plan.provenance is not None and plan.provenance.component == "topic_planner"
    params = llm.calls[0]["tools"][0]["function"]["parameters"]
    assert not {"topic_id", "cefr", "provenance"} & set(params["properties"])


async def test_plan_topic_feeds_coverage_errors_back_once() -> None:
    args = _plan_args()
    uncovered = {**args, "steps": [args["steps"][0]]}  # drops the second step's rule points and nuances
    llm = ScriptedLLM([submit(uncovered), submit(args)])
    plan = await plan_topic(llm, TOPIC, NOTES)
    assert len(plan.steps) == 2
    feedback = llm.calls[1]["messages"][-1]["content"]
    assert "not assigned to any step" in feedback


async def test_plan_topic_gives_up_after_attempts() -> None:
    args = _plan_args()
    uncovered = {**args, "steps": [args["steps"][0]]}
    llm = ScriptedLLM([submit(uncovered), submit(uncovered)])
    with pytest.raises(LLMWriteError):
        await plan_topic(llm, TOPIC, NOTES)


def test_plan_problems_rejects_out_of_range_and_duplicate_ids() -> None:
    plan = TopicPlan.model_validate({**topic_content_payload()["plan"]})
    duplicated = plan.model_copy(
        update={"steps": [plan.steps[0], plan.steps[1].model_copy(update={"rule_point_ids": [0, 2, 3]})]}
    )
    assert "more than one step" in plan_problems(duplicated, NOTES)


def test_plan_problems_empty_when_covered() -> None:
    plan = TopicPlan.model_validate({**topic_content_payload()["plan"]})
    assert plan_problems(plan, NOTES) == ""


def test_plan_rejects_reserved_slug() -> None:
    args = _plan_args()
    args["steps"][0]["slug"] = "core"
    with pytest.raises(ValidationError):
        TopicPlan.model_validate({**args, "topic_id": "there_is_are", "cefr": "A2"})


async def test_a_fixed_step_count_is_asked_for_and_enforced() -> None:
    args = _plan_args()
    llm = ScriptedLLM([submit(args), submit(args)])
    with pytest.raises(LLMWriteError, match="exactly 3 step"):
        await plan_topic(llm, TOPIC, NOTES, steps=3)
    assert "Target number of steps: 3" in llm.calls[0]["messages"][1]["content"]
