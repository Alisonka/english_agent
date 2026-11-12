from __future__ import annotations

from typing import Any

import pytest

from english_agent.grader import GradeError, _submit_tool, grade_translation
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.lesson import Lesson
from fakes import ScriptedLLM

GOOD: dict[str, Any] = {
    "sentences": [
        {
            "n": 1,
            "learner_text": "There is a window in my room.",
            "verdict": "correct",
            "corrected": "There is a window in my room.",
            "explanation_ru": "",
        },
        {
            "n": 2,
            "learner_text": "There is no children in the park.",
            "verdict": "fixed",
            "corrected": "There are no children in the park.",
            "explanation_ru": "Children — множественное число, поэтому there are.",
            "error_type": "target_form",
        },
        {"n": 3, "learner_text": "", "verdict": "missing", "corrected": "Is there a cafe here?", "explanation_ru": ""},
    ],
    "focus_ru": "Смотри на число существительного после there.",
}


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="g1", name="submit_grade", arguments=args)])


async def test_grade_has_code_owned_provenance_and_sees_the_task(lesson: Lesson) -> None:
    llm = ScriptedLLM([submit({**GOOD, "provenance": {"component": "fake"}})])

    grade = await grade_translation(llm, lesson, "There is a window in my room.")

    assert grade.correct_count == 1
    assert grade.provenance is not None and grade.provenance.component == "grader"
    assert grade.provenance.model == "claude-haiku-5-5"
    prompt = llm.transcripts[0][1]["content"]
    assert lesson.translation[1].reference_en in prompt
    assert "<<<\nThere is a window in my room.\n>>>" in prompt
    assert "provenance" not in _submit_tool()["function"]["parameters"]["properties"]


async def test_invalid_grade_is_fed_back_once(lesson: Lesson) -> None:
    bad = {**GOOD, "sentences": GOOD["sentences"][:2]}
    llm = ScriptedLLM([submit(bad), submit(GOOD)])

    await grade_translation(llm, lesson, "answer")

    assert llm.transcripts[1][-1]["role"] == "tool"
    assert "sentences" in llm.transcripts[1][-1]["content"]


async def test_grader_gives_up_after_attempts(lesson: Lesson) -> None:
    fixed_without_reason = {
        **GOOD,
        "sentences": [{**GOOD["sentences"][1], "explanation_ru": ""}, *GOOD["sentences"][::2]],
    }
    llm = ScriptedLLM([submit(fixed_without_reason), submit(fixed_without_reason)])
    with pytest.raises(GradeError):
        await grade_translation(llm, lesson, "answer")


async def test_lesson_without_translation_is_rejected(lesson: Lesson) -> None:
    with pytest.raises(GradeError):
        await grade_translation(ScriptedLLM([]), lesson.model_copy(update={"translation": []}), "answer")
