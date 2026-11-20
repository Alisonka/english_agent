"""The Exercise composer: quiz questions written to order for one learner's gap, checked independently.

A writer model composes the questions; the Critic (another model) answers each one without seeing
the key, and code drops every question whose key it does not confirm. The Critic also rates each
question's difficulty, which becomes its starting difficulty. A question with a personal theme stays
the learner's own; the others join the item's shared bank.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from english_agent.knowledge import critic_difficulty, question_key
from english_agent.llm import ChatLLM, LLMWriteError, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.exercise import BankExercise, ComposedExercises, ExerciseOrder, IndependentAnswers
from english_agent.models.storage import ItemRecord

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_WRITE = (_PROMPTS / "compose_exercises_system.md").read_text(encoding="utf-8")
_CHECK = (_PROMPTS / "check_exercises_system.md").read_text(encoding="utf-8")


async def compose_exercises(
    writer: ChatLLM,
    critic: ChatLLM,
    order: ExerciseOrder,
    item: ItemRecord,
    *,
    cefr: Cefr,
    learner_id: int,
    now: datetime,
) -> tuple[list[BankExercise], dict[str, float]]:
    """The exercises that passed the independent check, and each one's starting difficulty by key."""
    composed = await submit_model(
        writer,
        ComposedExercises,
        tool=tool_schema(ComposedExercises, name="submit_exercises", description="Submit the composed questions."),
        messages=[{"role": "system", "content": _WRITE}, {"role": "user", "content": _order_brief(order, item, cefr)}],
        build=lambda arguments: arguments,
        problem=lambda found: "" if len(found.questions) == order.count else f"write exactly {order.count} questions",
        span_name="composer.write",
        span_attrs={"exercise.kind": order.kind, "item.key": item.key},
    )
    questions = composed.questions

    def check(answers: IndependentAnswers) -> str:
        if len(answers.keys) != len(questions) or len(answers.difficulty) != len(questions):
            return f"give one key and one difficulty per question ({len(questions)}), in order"
        return ""

    answers = await submit_model(
        critic,
        IndependentAnswers,
        tool=tool_schema(IndependentAnswers, name="submit_answers", description="Your answer and rating per question."),
        messages=[{"role": "system", "content": _CHECK}, {"role": "user", "content": _blind(questions, cefr)}],
        build=lambda arguments: arguments,
        problem=check,
        span_name="composer.check",
        span_attrs={"exercise.count": len(questions)},
    )
    personal = learner_id if order.theme else None
    kept: list[BankExercise] = []
    priors: dict[str, float] = {}
    for question, key, rating in zip(questions, answers.keys, answers.difficulty, strict=True):
        if key != question.correct_option:
            continue  # the Critic chose another option: the key is wrong or the question ambiguous
        exercise_key = question_key(item.key, question.question)
        kept.append(
            BankExercise(
                question_key=exercise_key,
                item_id=item.id,
                item_key=item.key,
                topic_id=item.topic_id,
                kind=order.kind,
                question=question,
                learner_id=personal,
                created_at=now,
            )
        )
        priors[exercise_key] = critic_difficulty(cefr, rating)
    if not kept:
        raise LLMWriteError("composer: the Critic confirmed none of the composed questions")
    return kept, priors


def _order_brief(order: ExerciseOrder, item: ItemRecord, cefr: Cefr) -> str:
    lines = [
        f"Level: {cefr}. Item: {item.key} — form: {item.form}; what it is for: {item.function_ru}.",
        f"Kind: {order.kind}. Questions: {order.count}. Target difficulty: {order.target_difficulty} of 5.",
    ]
    if order.error_type:
        lines.append(f"The learner's error type: {order.error_type}.")
    if order.hypothesis_ru:
        lines.append(f"Hypothesis to test: {order.hypothesis_ru}")
    if order.theme:
        lines.append(f"Theme the learner likes (use it in the sentences): {order.theme}.")
    return "\n".join(lines)


def _blind(questions: list[Any], cefr: Cefr) -> str:
    lines = [f"Level: {cefr}. Answer every question; the keys are hidden."]
    for number, question in enumerate(questions):
        options = "; ".join(f"{index}: {option}" for index, option in enumerate(question.options))
        lines.append(f"[{number}] {question.question} — {options}")
    return "\n".join(lines)
