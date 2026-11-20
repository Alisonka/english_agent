from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from english_agent.diagnostician import diagnose
from english_agent.llm import AssistantTurn, LLMWriteError, ToolCall
from english_agent.models.profile import Reflection
from english_agent.models.storage import EvidenceRecord, ItemAccuracy, ProfileInputs
from english_agent.reflector import ReflectionRequest, reflection_due
from english_agent.topic_graph import default_graph
from fakes import FakeEvidence, ScriptedLLM
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


def call(name: str, args: dict[str, Any] | None = None, call_id: str = "c") -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args or {})])


async def run(llm: ScriptedLLM, reader: FakeEvidence | None = None) -> Reflection:
    base = request()
    return await diagnose(
        llm,
        learner_id=1,
        inputs=base.inputs,
        accuracy=base.accuracy,
        previous=None,
        now=NOW,
        reader=reader,
        graph=default_graph(),
    )


def hypothesis(keys: list[str], evidence: list[int]) -> dict[str, Any]:
    return {
        **point(keys, evidence),
        "cause": "form",
        "probe_ru": "Выбери: There is / It is a cat on the sofa.",
        "intervention": "contrast",
    }


async def test_the_diagnosis_looks_at_the_data_first_and_code_owns_the_bookkeeping() -> None:
    reader = FakeEvidence()
    reader.records.append(
        EvidenceRecord(
            id=11,
            learner_id=1,
            kind="quiz_answer",
            source_ref="quiz_answer:11",
            topic_id="be_present",
            item_id=None,
            error_id=None,
            payload={"is_correct": False},
            occurred_at=NOW,
            cancelled_at=None,
        )
    )
    llm = ScriptedLLM(
        [
            call("list_errors", {"item_key": "there_is_are:core"}),
            call("prerequisites", {"item_key": "there_is_are:core"}),
            call("submit_diagnosis", {"focus_points": [hypothesis(["there_is_are:core"], [1, 2])], "notes_ru": "x"}),
        ]
    )

    result = await run(llm, reader)

    assert result.focus_keys == {"there_is_are:core"} and result.focus_points[0].cause == "form"
    assert (result.based_on_errors, result.until_error_id, result.reflected_at) == (3, 3, NOW)
    assert result.provenance is not None and result.provenance.component == "diagnostician"
    listed, prerequisites = (json.loads(m["content"]) for m in llm.transcripts[2] if m["role"] == "tool")
    assert [line["id"] for line in listed] == [2, 1]
    assert prerequisites["prerequisites"] == [{"topic": "be_present", "answers": 1, "correct": 0}]
    submit_params = llm.calls[0]["tools"][-1]["function"]["parameters"]["properties"]
    assert not {"based_on_errors", "until_error_id", "reflected_at", "provenance"} & set(submit_params)


async def test_a_hypothesis_must_rest_on_errors_of_its_own_items_and_name_its_probe() -> None:
    no_probe = point(["there_is_are:core"], [1])
    llm = ScriptedLLM(
        [
            call("submit_diagnosis", {"focus_points": [hypothesis(["there_is_are:core"], [3])]}),  # 3 is articles
            call("submit_diagnosis", {"focus_points": [no_probe]}),
        ]
    )
    with pytest.raises(LLMWriteError, match="give cause, probe_ru and intervention"):
        await run(llm)
    assert "belongs to articles:core" in llm.transcripts[1][-1]["content"]


async def test_without_a_history_reader_the_tools_say_so_and_unknown_items_are_refused() -> None:
    llm = ScriptedLLM(
        [
            call("item_history", {"item_key": "there_is_are:core"}),
            call("item_history", {"item_key": "nope:core"}),
            call("submit_diagnosis", {"focus_points": [], "notes_ru": ""}),
        ]
    )
    result = await run(llm)
    assert result.focus_points == [] and result.until_error_id == 3
    first, second = (json.loads(m["content"]) for m in llm.transcripts[2] if m["role"] == "tool")
    assert "not available" in first["error"] and "unknown item_key" in second["error"]


async def test_a_diagnosis_that_never_comes_fails_the_run() -> None:
    llm = ScriptedLLM([call("list_errors")] * 6)
    with pytest.raises(LLMWriteError, match="no diagnosis after 6 steps"):
        await run(llm)
