"""The Critic: a second model checks a written step or cheat sheet before any learner sees it.

It sees what the writer was asked to teach (the plan entry, this step's note points, the other
steps' titles, the topic's known false claims from the golden set) and the written step with the
quiz keys hidden. It answers every quiz question itself and lists issues; code turns a differing
answer into a blocking `wrong_key` issue and decides the verdict. The content runner sends a
`revise` back to the writer once with `revision_feedback`, and gives up on a second one.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from english_agent.llm import ChatLLM, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.golden import GoldenCase, load_goldens_yaml
from english_agent.models.review import ReviewIssue, StepReview, SummaryReview
from english_agent.models.topic import LessonStep, TopicPlan, TopicSummary
from english_agent.research.models import TopicNotes
from english_agent.step_writer import notes_for_step
from english_agent.summary_writer import steps_digest
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_STEP_PROMPT = (_PROMPTS / "review_step_system.md").read_text(encoding="utf-8")
_SUMMARY_PROMPT = (_PROMPTS / "review_summary_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("verdict", "provenance")

# What a rewrite is shown of its previous attempt: the writer's own fields, not the code-owned ones.
STEP_FIELDS = {"message", "practice"}
SUMMARY_FIELDS = {"title_en", "title_ru", "sections", "forms", "top_mistakes_ru"}

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _goldens() -> dict[str, GoldenCase]:
    try:
        return {case.topic_id: case for case in load_goldens_yaml()}
    except FileNotFoundError:
        log.warning("golden set not found: the Critic reviews without known false claims")
        return {}


def golden_for(topic_id: str) -> GoldenCase | None:
    return _goldens().get(topic_id)


def _blocking(issues: list[Any]) -> bool:
    return any(isinstance(issue, dict) and issue.get("blocking") is True for issue in issues)


async def review_step(
    llm: ChatLLM,
    step: LessonStep,
    plan: TopicPlan,
    notes: TopicNotes,
    *,
    main: LessonStep | None = None,
    cefr: Cefr = "A2",
) -> StepReview:
    """Review one written step; `main` is the main step when `step` is its `alt` re-teach."""
    questions = step.practice.quiz if step.practice is not None else []
    provenance = build_provenance(
        component="critic",
        schema_version=SCHEMA_VERSIONS["step_review"],
        model=getattr(llm, "model", None),
        prompt_names=("review_step_system",),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _STEP_PROMPT},
        {"role": "user", "content": _step_brief(step, plan, notes, main=main, cefr=cefr)},
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        issues = list(arguments.get("issues") or [])
        keys = arguments.get("quiz_keys")
        if isinstance(keys, list) and len(keys) == len(questions):
            for index, (key, question) in enumerate(zip(keys, questions, strict=True)):
                if key != question.correct_option:
                    issues.append(
                        {
                            "where": f"practice.quiz[{index}]",
                            "kind": "wrong_key",
                            "blocking": True,
                            "fix": (
                                f"An independent check chose option {key}, the key says {question.correct_option}: "
                                "make exactly one option correct and point correct_option at it."
                            ),
                        }
                    )
        verdict = "revise" if _blocking(issues) else "accept"
        return {**arguments, "issues": issues, "verdict": verdict, "provenance": provenance}

    def problem(review: StepReview) -> str:
        if len(review.quiz_keys) != len(questions):
            return f"quiz_keys must hold exactly one answer per quiz question ({len(questions)}), in order"
        return ""

    return await submit_model(
        llm,
        StepReview,
        tool=tool_schema(
            StepReview,
            name="submit_review",
            description="Submit your review of the step: your answer to every quiz question and the issues.",
            owned=_OWNED_BY_CODE,
        ),
        messages=messages,
        build=build,
        problem=problem,
        span_name="critic.review_step",
        span_attrs={"topic.id": step.topic_id, "step.index": step.index, "step.variant": step.variant},
    )


async def review_summary(
    llm: ChatLLM, summary: TopicSummary, steps: list[LessonStep], *, cefr: Cefr = "A2"
) -> SummaryReview:
    """Review a topic's cheat sheet against the steps it summarises."""
    provenance = build_provenance(
        component="critic",
        schema_version=SCHEMA_VERSIONS["summary_review"],
        model=getattr(llm, "model", None),
        prompt_names=("review_summary_system",),
    )
    sheet = json.dumps(summary.model_dump(mode="json", include=SUMMARY_FIELDS), ensure_ascii=False, indent=1)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SUMMARY_PROMPT},
        {
            "role": "user",
            "content": (
                f"Level: {cefr}. Topic: {summary.topic_id}.\n\nThe steps:{steps_digest(steps)}\n\n"
                f"The cheat sheet (JSON):\n{sheet}"
            ),
        },
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        issues = list(arguments.get("issues") or [])
        verdict = "revise" if _blocking(issues) else "accept"
        return {**arguments, "issues": issues, "verdict": verdict, "provenance": provenance}

    return await submit_model(
        llm,
        SummaryReview,
        tool=tool_schema(
            SummaryReview,
            name="submit_review",
            description="Submit your review of the cheat sheet: the issues.",
            owned=_OWNED_BY_CODE,
        ),
        messages=messages,
        build=build,
        span_name="critic.review_summary",
        span_attrs={"topic.id": summary.topic_id},
    )


def revision_feedback(previous: BaseModel, issues: list[ReviewIssue], *, include: set[str]) -> str:
    """What a rewrite is told: its previous attempt and the Critic's numbered issues."""
    lines = [
        "",
        "A reviewer checked your previous attempt. Write it again: fix every MUST FIX problem, fix the",
        "optional ones where you can, and keep everything else as it was.",
        "Previous attempt (JSON):",
        json.dumps(previous.model_dump(mode="json", include=include), ensure_ascii=False),
        "Problems:",
    ]
    for number, issue in enumerate(issues, start=1):
        tag = "MUST FIX" if issue.blocking else "optional"
        lines.append(f"{number}. [{tag}] {issue.kind} at {issue.where}: {issue.fix}")
    return "\n".join(lines)


def _step_for_review(step: LessonStep) -> dict[str, Any]:
    """The step as the Critic sees it: the writer's fields, the quiz without its key and explanations."""
    data: dict[str, Any] = {"variant": step.variant, "message": step.message.model_dump(mode="json")}
    if step.practice is not None:
        data["practice"] = {
            "quiz": [
                {"kind": q.kind, "question": q.question, "options": q.options, "item_slug": q.item_slug}
                for q in step.practice.quiz
            ],
            "translation": [sentence.model_dump(mode="json") for sentence in step.practice.translation],
        }
    return data


def _step_brief(step: LessonStep, plan: TopicPlan, notes: TopicNotes, *, main: LessonStep | None, cefr: Cefr) -> str:
    plan_step = plan.steps[step.index - 1]
    others = [f"{i}. {other.title_en} ({other.slug})" for i, other in enumerate(plan.steps, 1) if i != step.index]
    lines = [
        f"Level: {cefr}. Topic: {plan.topic_id}. This is step {step.index} of {step.total} ({step.variant}).",
        f"Plan entry: {plan_step.title_en} / {plan_step.title_ru} (slug {plan_step.slug}).",
        f"Step idea: {plan_step.key_idea_ru}",
        f"Introduces a new form: {'yes' if plan_step.introduces_form else 'no, it reuses an earlier form'}.",
        "Other steps of the topic: " + ("; ".join(others) if others else "none"),
        "",
        *notes_for_step(plan_step, notes),
    ]
    golden = golden_for(plan.topic_id)
    if golden is not None:
        lines.append("Known false claims about this topic (the step must not say or imply them):")
        lines.extend(f"- {claim}" for claim in golden.forbidden_claims)
        error = golden.typical_l1_ru_error
        lines.append(f"A typical Russian-speaker mistake here: {error.incorrect} -> {error.correct}")
    if main is not None:
        lines.append("")
        lines.append("The main step this re-teach re-explains (JSON):")
        lines.append(json.dumps(main.message.model_dump(mode="json"), ensure_ascii=False))
    lines.append("")
    lines.append("The step to review (JSON):")
    lines.append(json.dumps(_step_for_review(step), ensure_ascii=False, indent=1))
    return "\n".join(lines)
