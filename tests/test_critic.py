from __future__ import annotations

import json
from typing import Any

from conftest import topic_content_payload
from english_agent.critic import STEP_FIELDS, review_step, review_summary, revision_feedback
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.review import ReviewIssue
from english_agent.models.topic import LessonStep, TopicContent
from fakes import ScriptedLLM
from test_topic_planner import NOTES

CONTENT = TopicContent.model_validate(topic_content_payload())
MAIN = CONTENT.step(1, "main")
ALT = CONTENT.step(1, "alt")


def submit(args: dict[str, Any]) -> AssistantTurn:
    """A review call; a step review without `quiz_difficulty` rates every question 3 (typical)."""
    if "quiz_keys" in args and "quiz_difficulty" not in args:
        args = {**args, "quiz_difficulty": [3] * len(args["quiz_keys"])}
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="r1", name="submit_review", arguments=args)])


def keys_of(step: LessonStep) -> list[int]:
    assert step.practice is not None
    return [question.correct_option for question in step.practice.quiz]


def issue(blocking: bool, kind: str = "wrong_rule") -> dict[str, Any]:
    return {"where": "message.key_idea_ru", "kind": kind, "blocking": blocking, "fix": "Say it the right way."}


async def test_a_correct_step_is_accepted_and_the_key_stays_hidden() -> None:
    llm = ScriptedLLM([submit({"issues": [issue(False, "unclear_ru")], "quiz_keys": keys_of(MAIN)})])

    review = await review_step(llm, MAIN, CONTENT.plan, NOTES)

    assert review.verdict == "accept" and len(review.issues) == 1
    assert review.provenance is not None and review.provenance.component == "critic"
    brief = llm.transcripts[0][1]["content"]
    assert "correct_option" not in brief
    assert MAIN.practice is not None and MAIN.practice.quiz[0].explanation_ru not in brief
    params = llm.calls[0]["tools"][0]["function"]["parameters"]["properties"]
    assert "verdict" not in params and "provenance" not in params


async def test_a_quiz_key_the_critic_disagrees_with_blocks_the_step() -> None:
    keys = keys_of(MAIN)
    assert MAIN.practice is not None
    keys[2] = (keys[2] + 1) % len(MAIN.practice.quiz[2].options)
    llm = ScriptedLLM([submit({"issues": [], "quiz_keys": keys})])

    review = await review_step(llm, MAIN, CONTENT.plan, NOTES)

    assert review.verdict == "revise"
    [wrong] = review.blocking
    assert wrong.kind == "wrong_key" and wrong.where == "practice.quiz[2]"


async def test_a_blocking_issue_means_revise_whatever_the_model_thinks() -> None:
    llm = ScriptedLLM([submit({"verdict": "accept", "issues": [issue(True)], "quiz_keys": keys_of(MAIN)})])
    review = await review_step(llm, MAIN, CONTENT.plan, NOTES)
    assert review.verdict == "revise"


async def test_missing_quiz_answers_are_asked_for_again() -> None:
    llm = ScriptedLLM(
        [
            submit({"issues": [], "quiz_keys": keys_of(MAIN)[:2]}),
            submit({"issues": [], "quiz_keys": keys_of(MAIN)}),
        ]
    )
    review = await review_step(llm, MAIN, CONTENT.plan, NOTES)
    assert review.verdict == "accept"
    assert "one answer per quiz question" in llm.transcripts[1][-1]["content"]


async def test_a_review_must_rate_every_quiz_question() -> None:
    keys = keys_of(MAIN)
    llm = ScriptedLLM(
        [
            submit({"issues": [], "quiz_keys": keys, "quiz_difficulty": [3]}),
            submit({"issues": [], "quiz_keys": keys, "quiz_difficulty": [5] * len(keys)}),
        ]
    )
    review = await review_step(llm, MAIN, CONTENT.plan, NOTES)
    assert review.quiz_difficulty == [5] * len(keys)
    assert "quiz_difficulty must rate every quiz question" in llm.transcripts[1][-1]["content"]


async def test_an_alt_step_is_reviewed_against_its_main_step() -> None:
    llm = ScriptedLLM([submit({"issues": [], "quiz_keys": []})])
    review = await review_step(llm, ALT, CONTENT.plan, NOTES, main=MAIN)
    assert review.verdict == "accept"
    brief = llm.transcripts[0][1]["content"]
    assert "re-teach re-explains" in brief and MAIN.message.hook_ru in brief


async def test_the_golden_sets_false_claims_reach_the_critic() -> None:
    llm = ScriptedLLM([submit({"issues": [], "quiz_keys": keys_of(MAIN)})])
    await review_step(llm, MAIN, CONTENT.plan, NOTES)
    assert "Known false claims" in llm.transcripts[0][1]["content"]  # there_is_are is in the golden set


async def test_the_cheat_sheet_review_decides_by_blocking_issues() -> None:
    llm = ScriptedLLM([submit({"issues": [issue(True, "off_step")]})])
    review = await review_summary(llm, CONTENT.summary, CONTENT.steps)
    assert review.verdict == "revise" and review.blocking[0].kind == "off_step"
    assert CONTENT.steps[0].title_en in llm.transcripts[0][1]["content"]


def test_revision_feedback_carries_the_previous_attempt_and_the_numbered_issues() -> None:
    issues = [ReviewIssue.model_validate(issue(True)), ReviewIssue.model_validate(issue(False, "unclear_ru"))]
    feedback = revision_feedback(MAIN, issues, include=STEP_FIELDS)
    assert "1. [MUST FIX] wrong_rule at message.key_idea_ru" in feedback
    assert "2. [optional] unclear_ru" in feedback
    previous = json.loads(feedback.split("Previous attempt (JSON):\n", 1)[1].split("\nProblems:", 1)[0])
    assert set(previous) == {"message", "practice"}
