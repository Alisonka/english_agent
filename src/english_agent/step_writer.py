"""write_step: one step of a topic plan in, one validated LessonStep out.

The model sees only this step's plan entry and note points, a recap of earlier steps, and the next
step's title for the teaser — never the material of other steps, so it cannot leak tomorrow into
today. Code owns the structural fields (slug, index, total, variant, sources) and the Critic's
`review`. The `alt` variant re-teaches the same idea with a fresh hook and different examples and
carries no practice. `feedback` (the Critic's issues with the previous attempt) asks for a rewrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from english_agent.llm import ChatLLM, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Source
from english_agent.models.topic import LessonStep, PlanStep, StepVariant, TopicPlan
from english_agent.research.models import TopicNotes
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "write_step_system.md").read_text(encoding="utf-8")
_OWNED_BASE = (
    "topic_id",
    "title_en",
    "title_ru",
    "cefr",
    "slug",
    "index",
    "total",
    "variant",
    "sources",
    "provenance",
    "review",
)


def _slice(points: list[Any], ids: list[int]) -> list[Any]:
    return [points[index] for index in ids if 0 <= index < len(points)]


async def write_step(
    llm: ChatLLM,
    plan: TopicPlan,
    index: int,
    notes: TopicNotes,
    *,
    variant: StepVariant = "main",
    main: LessonStep | None = None,
    content_so_far: list[LessonStep] | None = None,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
    feedback: str | None = None,
) -> LessonStep:
    """Write step `index` (1-based) of `plan` in the given variant; `main` is the main step for an alt.

    `feedback` is appended to the brief when the Critic sent the previous attempt back.
    """
    total = len(plan.steps)
    plan_step = plan.steps[index - 1]
    previous = plan.steps[: index - 1]
    allowed_slugs = {plan_step.slug, *(step.slug for step in previous)}
    is_last = index == total
    provenance = build_provenance(
        component="step_writer",
        schema_version=SCHEMA_VERSIONS["lesson_step"],
        model=getattr(llm, "model", None),
        prompt_names=("write_step_system",),
        curriculum_version=curriculum_version,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {
            "role": "user",
            "content": _step_brief(plan, index, notes, variant=variant, main=main, content_so_far=content_so_far or [])
            + (f"\n{feedback}" if feedback else ""),
        },
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **arguments,
            "topic_id": plan.topic_id,
            "title_en": plan_step.title_en,
            "title_ru": plan_step.title_ru,
            "cefr": cefr,
            "slug": plan_step.slug,
            "index": index,
            "total": total,
            "variant": variant,
            "sources": [source.model_dump(mode="json") for source in _sources(notes)],
            "provenance": provenance,
        }
        if variant == "alt":
            payload["practice"] = None
        return payload

    owned = _OWNED_BASE + (("practice",) if variant == "alt" else ())
    return await submit_model(
        llm,
        LessonStep,
        tool=tool_schema(
            LessonStep,
            name="submit_step",
            description="Submit the step's message and practice bank.",
            owned=owned,
        ),
        messages=messages,
        build=build,
        problem=lambda step: _step_problem(step, allowed_slugs=allowed_slugs, is_last=is_last, main=main),
        span_name="writer.write_step",
        span_attrs={
            "topic.id": plan.topic_id,
            "step.index": index,
            "step.variant": variant,
            "step.rewrite": feedback is not None,
        },
    )


def _sources(notes: TopicNotes) -> list[Source]:
    return [notes.source]


def _step_problem(step: LessonStep, *, allowed_slugs: set[str], is_last: bool, main: LessonStep | None) -> str:
    problems: list[str] = []
    if is_last and step.message.teaser_ru is not None:
        problems.append("the last step must not have a teaser_ru")
    if not is_last and step.message.teaser_ru is None:
        problems.append("every step but the last needs a teaser_ru naming the next step")
    if step.practice is not None:
        for sentence in step.practice.translation:
            if sentence.item_slug != step.slug:
                problems.append(f"translation item_slug {sentence.item_slug!r} must be this step's slug")
        bad = sorted({q.item_slug for q in step.practice.quiz if q.item_slug not in allowed_slugs})
        if bad:
            problems.append(f"quiz item_slug not this or an earlier step: {bad}")
    if step.variant == "alt" and main is not None:
        main_examples = {example.en for example in main.message.examples}
        if any(example.en in main_examples for example in step.message.examples):
            problems.append("an alt step must use different example sentences from the main step")
    return "; ".join(problems)


def _step_brief(
    plan: TopicPlan,
    index: int,
    notes: TopicNotes,
    *,
    variant: StepVariant,
    main: LessonStep | None,
    content_so_far: list[LessonStep],
) -> str:
    plan_step = plan.steps[index - 1]
    total = len(plan.steps)
    lines = [
        f"Topic: {plan.topic_id} (level {plan.cefr}). Characters: {', '.join(plan.characters)}.",
        f"This is step {index} of {total}: {plan_step.title_en} / {plan_step.title_ru}.",
        f"Step idea: {plan_step.key_idea_ru}",
        f"Introduces a new form: {'yes' if plan_step.introduces_form else 'no, reuse the earlier form'}.",
    ]
    if content_so_far:
        recap = "; ".join(f"{step.title_en}: {step.message.key_idea_ru}" for step in content_so_far)
        lines.append(f"Earlier steps (do not re-teach): {recap}")
    if index < total:
        lines.append(f"Next step (for the teaser): {plan.steps[index].title_en} / {plan.steps[index].title_ru}")
    else:
        lines.append("This is the LAST step: no teaser_ru.")
    lines.append("")
    lines.extend(notes_for_step(plan_step, notes))
    if variant == "alt":
        lines.append("")
        lines.append("Write the ALT (re-teach) variant: a new hook, the same idea in other words, 3 DIFFERENT")
        lines.append("examples, and NO practice.")
        if main is not None:
            lines.append(
                "The main step's examples (use different ones): " + "; ".join(e.en for e in main.message.examples)
            )
    return "\n".join(lines)


def notes_for_step(plan_step: PlanStep, notes: TopicNotes) -> list[str]:
    """The research note points the plan gave this step, as brief sections (the writer's and the Critic's)."""
    lines = [f"Rule points for this step:\n{_bullets(_slice(notes.rule_points, plan_step.rule_point_ids))}"]
    if nuances := _slice(notes.nuances, plan_step.nuance_ids):
        lines.append(f"Nuances for this step:\n{_bullets(nuances)}")
    if examples := _slice(notes.examples, plan_step.example_ids):
        lines.append("Example material:\n" + _bullets([f"{e.en}" + (f" — {e.ru}" if e.ru else "") for e in examples]))
    if mistakes := _slice(notes.typical_mistakes, plan_step.mistake_ids):
        lines.append(f"Typical mistakes:\n{_bullets(mistakes)}")
    return lines


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)
