from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from conftest import lesson_payload
from english_agent.interlocutor import AHEAD, EMPTY, LIMIT, OFF_TOPIC, answer_question
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord
from english_agent.storage import Stores
from fakes import FakeEvidence, ScriptedLLM, fake_stores

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def call(name: str, args: dict[str, Any] | None = None) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="t", name=name, arguments=args or {})])


async def learner_with_a_lesson() -> tuple[Stores, LearnerRecord]:
    stores, learners, lessons = fake_stores()
    learner = learners.started("42", date(2026, 11, 3))
    await lessons.record_sent(
        learner.id, date(2026, 11, 3), Lesson.model_validate(lesson_payload()), [1], curriculum_version=1
    )
    return stores, learner


def evidence(stores: Stores) -> FakeEvidence:
    assert isinstance(stores.evidence, FakeEvidence)
    return stores.evidence


async def ask(stores: Stores, learner: LearnerRecord, llm: ScriptedLLM, text: str = "Почему there are?") -> str:
    return await answer_question(llm, stores, learner, text, NOW, daily_limit=3)


async def test_a_studied_topic_is_answered_from_the_material_and_the_question_is_evidence() -> None:
    stores, learner = await learner_with_a_lesson()
    llm = ScriptedLLM(
        [
            call("studied_topics"),
            call("submit_answer", {"scope": "studied", "topic_id": "there_is_are", "answer_ru": "Потому что два."}),
        ]
    )

    assert await ask(stores, learner, llm) == "Потому что два."
    [studied] = json.loads([m for m in llm.transcripts[1] if m["role"] == "tool"][0]["content"])
    assert studied["topic_id"] == "there_is_are"
    assert "<learner_question>" in llm.transcripts[0][1]["content"]
    [recorded] = evidence(stores).records
    assert (recorded.kind, recorded.topic_id, recorded.payload["scope"]) == (
        "tutor_question",
        "there_is_are",
        "studied",
    )


async def test_a_topic_ahead_gets_the_honest_template_and_unstudied_material_is_refused() -> None:
    stores, learner = await learner_with_a_lesson()
    llm = ScriptedLLM(
        [
            call("topic_material", {"topic_id": "present_perfect_ever_never"}),
            call(
                "submit_answer", {"scope": "ahead", "topic_id": "present_perfect_ever_never", "answer_ru": "Вот урок…"}
            ),
        ]
    )

    assert await ask(stores, learner, llm, "Что такое Present Perfect?") == AHEAD
    refusal = json.loads([m for m in llm.transcripts[1] if m["role"] == "tool"][0]["content"])
    assert "not a studied topic" in refusal["error"]


async def test_off_topic_empty_and_over_the_limit() -> None:
    stores, learner = await learner_with_a_lesson()
    assert await ask(stores, learner, ScriptedLLM([]), "   ") == EMPTY
    off = ScriptedLLM([call("submit_answer", {"scope": "off_topic"})])
    assert await ask(stores, learner, off, "Какая погода?") == OFF_TOPIC
    for n in range(2):
        await stores.evidence.record(
            learner.id, "tutor_question", source_ref=f"t{n}", topic_id=None, payload={}, at=NOW
        )
    assert await ask(stores, learner, ScriptedLLM([])) == LIMIT  # 3 today: no model call
    assert await ask(stores, learner, ScriptedLLM([])) == LIMIT
    later = NOW + timedelta(days=1, minutes=1)
    tomorrow = await answer_question(
        ScriptedLLM([call("submit_answer", {"scope": "off_topic"})]), stores, learner, "x", later, daily_limit=3
    )
    assert tomorrow == OFF_TOPIC
