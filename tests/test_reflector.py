from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from english_agent.llm import AssistantTurn, LLMWriteError, ToolCall
from english_agent.models.storage import ItemAccuracy, ProfileInputs
from english_agent.reflector import ReflectionRequest, reflect, reflection_due
from fakes import ScriptedLLM
from test_profile import NOW, error, item, reflection


def inputs() -> ProfileInputs:
    return ProfileInputs(
        cefr="A2",
        window_days=7,
        errors=[error(1, 7, "target_form", 3), error(2, 7, "target_form", 2), error(3, 8, "article", 1)],
        items={7: item(7, "there_is_are"), 8: item(8, "articles")},
    )


def request() -> ReflectionRequest:
    accuracy = {7: ItemAccuracy(item_id=7, checks=5, correct=2, days=3)}
    return ReflectionRequest(inputs=inputs(), accuracy=accuracy, previous=None, now=NOW)


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="f1", name="submit_reflection", arguments=args)])


def point(keys: list[str], evidence: list[int]) -> dict[str, Any]:
    return {
        "item_keys": keys,
        "pattern_ru": "Путает there is и it is.",
        "contrast_ru": "There is — что-то есть где-то.",
        "error_type": "target_form",
        "evidence_error_ids": evidence,
    }


def test_a_first_reflection_waits_for_three_errors_with_an_item() -> None:
    two_with_an_item = [error(1, 7, "x", 1), error(2, None, "x", 1), error(3, 7, "x", 1)]
    assert not reflection_due(None, two_with_an_item, NOW)
    assert reflection_due(None, [error(n, 7, "x", 1) for n in range(1, 4)], NOW)


def test_at_most_about_one_reflection_a_day() -> None:
    recent = reflection(NOW - timedelta(hours=5))
    assert not reflection_due(recent, [error(n, 7, "x", 1) for n in range(1, 9)], NOW)


def test_only_errors_after_the_last_reflection_count() -> None:
    previous = reflection(NOW - timedelta(days=2), until_error_id=5)
    old = [error(n, 7, "x", 50) for n in range(1, 6)]
    assert not reflection_due(previous, old, NOW)
    assert not reflection_due(previous, [*old, error(6, 7, "x", 1)], NOW)
    assert reflection_due(previous, [*old, *(error(n, 7, "x", 1) for n in (6, 7, 8))], NOW)


def test_after_a_quiet_week_one_new_error_is_enough() -> None:
    previous = reflection(NOW - timedelta(days=8), until_error_id=5)
    assert reflection_due(previous, [error(6, 7, "x", 1)], NOW)


async def test_reflection_is_grounded_and_code_owns_the_bookkeeping() -> None:
    llm = ScriptedLLM(
        [submit({"focus_points": [point(["there_is_are:core"], [1, 2])], "notes_ru": "Следить за there."})]
    )

    result = await reflect(llm, request())

    assert result.focus_keys == {"there_is_are:core"}
    assert (result.based_on_errors, result.until_error_id, result.reflected_at) == (3, 3, NOW)
    assert result.provenance is not None and result.provenance.component == "reflector"
    brief = llm.transcripts[0][1]["content"]
    assert "[3] item articles:core" in brief and "there_is_are:core: 2/5 over 3 day(s)" in brief
    params = llm.calls[0]["tools"][0]["function"]["parameters"]["properties"]
    assert not {"based_on_errors", "until_error_id", "reflected_at", "provenance"} & set(params)


async def test_a_focus_point_must_rest_on_errors_of_its_own_items() -> None:
    llm = ScriptedLLM(
        [
            submit({"focus_points": [point(["there_is_are:core"], [3])]}),  # error 3 is an articles error
            submit({"focus_points": [point(["unknown:item"], [1])]}),
        ]
    )
    with pytest.raises(LLMWriteError, match="not among the given items"):
        await reflect(llm, request())
    assert "belongs to articles:core" in llm.transcripts[1][-1]["content"]


async def test_no_repeated_mistake_is_an_empty_reflection() -> None:
    llm = ScriptedLLM([submit({"focus_points": [], "notes_ru": ""})])
    result = await reflect(llm, request())
    assert result.focus_points == [] and result.until_error_id == 3
