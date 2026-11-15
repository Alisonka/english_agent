from __future__ import annotations

from typing import Any

import pytest

import english_agent.topic_writer as topic_writer
from conftest import topic_content_payload
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.topic import LessonStep, StepVariant, TopicContent, TopicPlan, TopicSummary
from english_agent.research.models import NOT_FOUND_MESSAGE, ResearchResult
from test_pipeline import make_settings
from test_planner import NOTES

TOPIC = CurriculumTopic(
    id="there_is_are", title_en="there is / there are", title_ru="there is / there are", order=13, tags=["there"]
)
CONTENT = TopicContent.model_validate(topic_content_payload())


def _found() -> ResearchResult:
    return ResearchResult(
        topic_id="there_is_are",
        status="found",
        message="found",
        notes=NOTES,
        sites_checked=["skysmart"],
        steps=1,
        trajectory=[],
    )


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    class FakeResearcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def run(self, topic: CurriculumTopic) -> ResearchResult:
            calls.append("research")
            return _found()

    async def fake_plan(llm: Any, topic: Any, notes: Any, **kwargs: Any) -> TopicPlan:
        calls.append("plan")
        return CONTENT.plan

    async def fake_write_step(
        llm: Any, plan: Any, index: int, notes: Any, *, variant: StepVariant = "main", **kwargs: Any
    ) -> LessonStep:
        calls.append(f"step:{index}:{variant}")
        return CONTENT.step(index, variant)

    async def fake_write_summary(llm: Any, plan: Any, steps: Any, notes: Any, **kwargs: Any) -> TopicSummary:
        calls.append("summary")
        return CONTENT.summary

    monkeypatch.setattr(topic_writer, "ResearchAgent", FakeResearcher)
    monkeypatch.setattr(topic_writer, "AnthropicLLM", lambda *a, **k: object())
    monkeypatch.setattr(topic_writer, "plan_topic", fake_plan)
    monkeypatch.setattr(topic_writer, "write_step", fake_write_step)
    monkeypatch.setattr(topic_writer, "write_summary", fake_write_summary)
    return calls


async def test_generate_topic_assembles_content_in_order(stub_stages: list[str]) -> None:
    research, content = await topic_writer.generate_topic(TOPIC, make_settings(), client=object())  # type: ignore[arg-type]
    assert research.status == "found"
    assert content is not None
    assert content.item_keys() == ["there_is_are:singular", "there_is_are:plural"]
    assert [step.variant for step in content.steps] == ["main", "main"]
    assert [step.variant for step in content.alternatives] == ["alt", "alt"]
    assert stub_stages == ["research", "plan", "step:1:main", "step:2:main", "step:1:alt", "step:2:alt", "summary"]


async def test_generate_topic_returns_none_when_research_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResearcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def run(self, topic: CurriculumTopic) -> ResearchResult:
            return ResearchResult(
                topic_id="there_is_are",
                status="not_found",
                message=NOT_FOUND_MESSAGE,
                notes=None,
                sites_checked=["skysmart", "skyeng", "englex"],
                steps=3,
                trajectory=[],
            )

    monkeypatch.setattr(topic_writer, "ResearchAgent", FakeResearcher)
    monkeypatch.setattr(topic_writer, "AnthropicLLM", lambda *a, **k: object())
    research, content = await topic_writer.generate_topic(TOPIC, make_settings(), client=object())  # type: ignore[arg-type]
    assert research.status == "not_found" and content is None
