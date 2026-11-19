"""write_summary: the whole topic in, the end-of-topic cheat sheet (TopicSummary) out.

The model summarises the steps already written; it adds nothing new. Code owns the sources and the
Critic's `review`; `feedback` (the Critic's issues with the previous attempt) asks for a rewrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from english_agent.llm import ChatLLM, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.topic import LessonStep, TopicPlan, TopicSummary
from english_agent.research.models import TopicNotes
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "write_summary_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("topic_id", "cefr", "sources", "provenance", "review")


async def write_summary(
    llm: ChatLLM,
    plan: TopicPlan,
    steps: list[LessonStep],
    notes: TopicNotes,
    *,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
    feedback: str | None = None,
) -> TopicSummary:
    provenance = build_provenance(
        component="summary_writer",
        schema_version=SCHEMA_VERSIONS["topic_summary"],
        model=getattr(llm, "model", None),
        prompt_names=("write_summary_system",),
        curriculum_version=curriculum_version,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": _summary_brief(plan, steps) + (f"\n{feedback}" if feedback else "")},
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            **arguments,
            "topic_id": plan.topic_id,
            "cefr": cefr,
            "sources": [notes.source.model_dump(mode="json")],
            "provenance": provenance,
        }

    def problem(summary: TopicSummary) -> str:
        if len(summary.sections) != len(plan.steps):
            return f"need exactly {len(plan.steps)} sections, one per step, got {len(summary.sections)}"
        return ""

    return await submit_model(
        llm,
        TopicSummary,
        tool=tool_schema(
            TopicSummary,
            name="submit_summary",
            description="Submit the end-of-topic cheat sheet.",
            owned=_OWNED_BY_CODE,
        ),
        messages=messages,
        build=build,
        problem=problem,
        span_name="writer.write_summary",
        span_attrs={"topic.id": plan.topic_id},
    )


def _summary_brief(plan: TopicPlan, steps: list[LessonStep]) -> str:
    header = f"Topic: {plan.topic_id} (level {plan.cefr}). Write one section per step, in this order."
    return header + "\n" + steps_digest(steps)


def steps_digest(steps: list[LessonStep]) -> str:
    """What each written step taught, compactly (the summary writer's and the Critic's view of the topic)."""
    lines: list[str] = []
    for step in steps:
        form = f" | form: {step.message.form}" if step.message.form else ""
        examples = "; ".join(example.en for example in step.message.examples)
        lines.append(
            f"\nStep {step.index} — {step.title_en} / {step.title_ru}{form}\n"
            f"  idea: {step.message.key_idea_ru}\n  examples: {examples}\n"
            f"  mistake: {step.message.incorrect} -> {step.message.correct}"
        )
    return "\n".join(lines)
