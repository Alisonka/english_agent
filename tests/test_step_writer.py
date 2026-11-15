from __future__ import annotations

import copy
from typing import Any

from conftest import topic_content_payload
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.topic import LessonStep, TopicPlan
from english_agent.step_writer import write_step
from fakes import ScriptedLLM
from samples import STEP_MESSAGE_SINGULAR, step_quiz, step_translation
from test_topic_planner import NOTES

PLAN = TopicPlan.model_validate(topic_content_payload()["plan"])


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="c1", name="submit_step", arguments=args)])


def _main_args() -> dict[str, Any]:
    return {
        "message": copy.deepcopy(STEP_MESSAGE_SINGULAR),
        "practice": {"quiz": step_quiz("singular"), "translation": step_translation("singular")},
    }


def _alt_message() -> dict[str, Any]:
    message = copy.deepcopy(STEP_MESSAGE_SINGULAR)  # keeps the teaser (step 1 of 2)
    message["hook_ru"] = "Другой заход: представь, что описываешь свою комнату другу."
    message["examples"] = [
        {"en": "There is a lamp here.", "ru": "Здесь лампа.", "highlights": ["There is"]},
        {"en": "There is a big window.", "ru": "Большое окно.", "highlights": ["There is"]},
        {"en": "There is a dog outside.", "ru": "На улице собака.", "highlights": ["There is"]},
    ]
    return message


async def test_write_step_main_returns_step_with_practice() -> None:
    llm = ScriptedLLM([submit(_main_args())])
    step = await write_step(llm, PLAN, 1, NOTES, variant="main")
    assert step.slug == "singular" and step.index == 1 and step.total == 2 and step.variant == "main"
    assert step.item_key == "there_is_are:singular"
    assert step.practice is not None and len(step.practice.quiz) == 5
    assert step.sources and str(step.sources[0].url) == str(NOTES.source.url)
    assert step.provenance is not None and step.provenance.component == "step_writer"
    params = llm.calls[0]["tools"][0]["function"]["parameters"]
    assert not {"topic_id", "slug", "index", "total", "variant", "sources"} & set(params["properties"])


async def test_write_step_rejects_a_foreign_bank_slug() -> None:
    bad = _main_args()
    bad["practice"]["quiz"][0]["item_slug"] = "stranger"  # not this step nor an earlier one
    llm = ScriptedLLM([submit(bad), submit(_main_args())])
    step = await write_step(llm, PLAN, 1, NOTES, variant="main")
    assert step.practice is not None
    assert "not this or an earlier step" in llm.calls[1]["messages"][-1]["content"]


async def test_write_step_alt_has_no_practice() -> None:
    main = await write_step(ScriptedLLM([submit(_main_args())]), PLAN, 1, NOTES, variant="main")
    llm = ScriptedLLM([submit({"message": _alt_message()})])
    alt = await write_step(llm, PLAN, 1, NOTES, variant="alt", main=main)
    assert alt.variant == "alt" and alt.practice is None
    assert {e.en for e in alt.message.examples}.isdisjoint({e.en for e in main.message.examples})
    params = llm.calls[0]["tools"][0]["function"]["parameters"]
    assert "practice" not in params["properties"]


async def test_write_step_alt_must_differ_from_main() -> None:
    main = await write_step(ScriptedLLM([submit(_main_args())]), PLAN, 1, NOTES, variant="main")
    reused = {"message": copy.deepcopy(STEP_MESSAGE_SINGULAR)}  # same examples as main
    llm = ScriptedLLM([submit(reused), submit({"message": _alt_message()})])
    alt = await write_step(llm, PLAN, 1, NOTES, variant="alt", main=main)
    assert alt.practice is None
    assert "different example" in llm.calls[1]["messages"][-1]["content"]


async def test_last_step_must_not_carry_a_teaser() -> None:
    args = {
        "message": {**copy.deepcopy(STEP_MESSAGE_SINGULAR), "teaser_ru": "лишний тизер"},
        "practice": {"quiz": step_quiz("plural"), "translation": step_translation("plural")},
    }
    good = {
        "message": {**copy.deepcopy(STEP_MESSAGE_SINGULAR), "teaser_ru": None},
        "practice": {"quiz": step_quiz("plural"), "translation": step_translation("plural")},
    }
    llm = ScriptedLLM([submit(args), submit(good)])
    step: LessonStep = await write_step(llm, PLAN, 2, NOTES, variant="main")
    assert step.index == 2 and step.message.teaser_ru is None
    assert "teaser" in llm.calls[1]["messages"][-1]["content"]  # the last-of-topic row was rejected and retried
