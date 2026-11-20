from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from english_agent.composer import compose_exercises
from english_agent.knowledge import critic_difficulty, question_key
from english_agent.llm import AssistantTurn, LLMWriteError, ToolCall
from english_agent.models.exercise import ExerciseOrder
from fakes import ScriptedLLM
from test_profile import item

NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
ITEM = item(7, "there_is_are")


def question(text: str, key: int = 1) -> dict[str, Any]:
    return {
        "kind": "gap_fill",
        "question": text,
        "options": ["is", "are", "be"],
        "correct_option": key,
        "explanation_ru": "Множественное число: are.",
    }


def call(name: str, args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="t", name=name, arguments=args)])


async def compose(writer: list[AssistantTurn], critic: list[AssistantTurn], **order: Any) -> Any:
    values: dict[str, Any] = {"kind": "probe", "item_key": ITEM.key, "count": 2}
    return await compose_exercises(
        ScriptedLLM(writer),
        ScriptedLLM(critic),
        ExerciseOrder.model_validate({**values, **order}),
        ITEM,
        cefr="A2",
        learner_id=5,
        now=NOW,
    )


async def test_only_questions_the_critic_answers_the_same_way_are_kept() -> None:
    written = call(
        "submit_exercises", {"questions": [question("There ___ two cats here."), question("There ___ a dog here.")]}
    )
    # The writer's key for the dog (1, "are") is wrong: the Critic answers 0, "is", and the question is dropped.
    checked = call("submit_answers", {"keys": [1, 0], "difficulty": [2, 4]})

    kept, priors = await compose([written], [checked])

    [exercise] = kept
    assert exercise.question.question == "There ___ two cats here." and exercise.learner_id is None  # shared
    assert priors == {question_key(ITEM.key, "There ___ two cats here."): critic_difficulty("A2", 2)}


async def test_a_themed_exercise_stays_the_learners_own_and_the_count_is_enforced() -> None:
    one = call("submit_exercises", {"questions": [question("There ___ two balls on the pitch.")]})
    two = call(
        "submit_exercises", {"questions": [question("There ___ two balls on the pitch."), question("There ___ goals.")]}
    )
    checked = call("submit_answers", {"keys": [1, 1], "difficulty": [3, 3]})

    kept, _ = await compose([one, two], [checked], theme="футбол")

    assert {e.learner_id for e in kept} == {5}


async def test_nothing_confirmed_fails_the_order() -> None:
    written = call("submit_exercises", {"questions": [question("There ___ two cats here.", key=0)]})
    with pytest.raises(LLMWriteError, match="confirmed none"):
        await compose([written], [call("submit_answers", {"keys": [1], "difficulty": [3]})], count=1)
