"""plan_topic: research notes in, a validated TopicPlan out.

The planner decides how many steps a topic becomes and which note points each step is built from.
It writes no lesson text. Coverage (every rule point and nuance assigned to exactly one step) is
checked in code and fed back to the model, the same one-retry loop the other stages use.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from english_agent.llm import ChatLLM, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.topic import MAX_STEPS, TopicPlan
from english_agent.research.models import TopicNotes
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "plan_topic_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("topic_id", "cefr", "provenance")
_POINTS_PER_STEP = 4


def step_budget(notes: TopicNotes) -> int:
    """How many steps a topic's notes suggest: ~one per few rule points and nuances, 1..MAX_STEPS."""
    weight = len(notes.rule_points) + len(notes.nuances)
    return max(1, min(MAX_STEPS, math.ceil(weight / _POINTS_PER_STEP)))


def plan_problems(plan: TopicPlan, notes: TopicNotes) -> str:
    """Why the plan does not cover the notes, or "" when it does. Fed back to the model."""
    problems: list[str] = []
    sizes = {
        "rule_point": (len(notes.rule_points), [step.rule_point_ids for step in plan.steps], True),
        "nuance": (len(notes.nuances), [step.nuance_ids for step in plan.steps], True),
        "example": (len(notes.examples), [step.example_ids for step in plan.steps], False),
        "mistake": (len(notes.typical_mistakes), [step.mistake_ids for step in plan.steps], False),
    }
    for name, (count, per_step, must_cover) in sizes.items():
        seen = [index for ids in per_step for index in ids]
        if out_of_range := sorted({index for index in seen if not 0 <= index < count}):
            problems.append(f"{name} ids out of range 0..{count - 1}: {out_of_range}")
        if duplicated := sorted({index for index in seen if seen.count(index) > 1}):
            problems.append(f"{name} ids assigned to more than one step: {duplicated}")
        if must_cover and (missing := sorted(set(range(count)) - set(seen))):
            problems.append(f"{name} ids not assigned to any step: {missing}")
    return "; ".join(problems)


async def plan_topic(
    llm: ChatLLM,
    topic: CurriculumTopic,
    notes: TopicNotes,
    *,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
) -> TopicPlan:
    provenance = build_provenance(
        component="planner",
        schema_version=SCHEMA_VERSIONS["topic_plan"],
        model=getattr(llm, "model", None),
        prompt_names=("plan_topic_system",),
        curriculum_version=curriculum_version,
    )
    target = step_budget(notes)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {
            "role": "user",
            "content": (
                f"Topic: {topic.title_en} / {topic.title_ru} (id: {topic.id}, level {cefr})\n"
                f"Target number of steps: {target}\n\n{_numbered_notes(notes)}"
            ),
        },
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        return {**arguments, "topic_id": topic.id, "cefr": cefr, "provenance": provenance}

    return await submit_model(
        llm,
        TopicPlan,
        tool=tool_schema(
            TopicPlan,
            name="submit_plan",
            description="Submit the topic plan: the ordered steps and their note points.",
            owned=_OWNED_BY_CODE,
        ),
        messages=messages,
        build=build,
        problem=lambda plan: plan_problems(plan, notes),
        span_name="planner.plan_topic",
        span_attrs={"topic.id": topic.id, "topic.target_steps": target},
    )


def _numbered_notes(notes: TopicNotes) -> str:
    """The notes with each point indexed, so the plan can reference points by id."""
    blocks = [f"Summary: {notes.summary_ru}"]
    blocks.append("Rule points:\n" + "\n".join(f"[r{i}] {point}" for i, point in enumerate(notes.rule_points)))
    if notes.nuances:
        blocks.append("Nuances:\n" + "\n".join(f"[n{i}] {point}" for i, point in enumerate(notes.nuances)))
    examples = "\n".join(
        f"[e{i}] {example.en}" + (f" — {example.ru}" if example.ru else "") for i, example in enumerate(notes.examples)
    )
    blocks.append("Examples:\n" + examples)
    if notes.typical_mistakes:
        blocks.append("Typical mistakes:\n" + "\n".join(f"[m{i}] {m}" for i, m in enumerate(notes.typical_mistakes)))
    return "\n\n".join(blocks)
