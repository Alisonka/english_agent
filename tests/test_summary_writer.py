from __future__ import annotations

from typing import Any

from conftest import topic_content_payload
from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.topic import TopicContent, TopicPlan
from english_agent.summary_writer import write_summary
from fakes import ScriptedLLM
from test_topic_planner import NOTES

_CONTENT = TopicContent.model_validate(topic_content_payload())
PLAN: TopicPlan = _CONTENT.plan
STEPS = _CONTENT.steps


def _summary_args() -> dict[str, Any]:
    summary = topic_content_payload()["summary"]
    return {key: summary[key] for key in ("title_en", "title_ru", "sections", "forms", "top_mistakes_ru")}


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id="c1", name="submit_summary", arguments=args)])


async def test_write_summary_returns_cheat_sheet_with_sources() -> None:
    llm = ScriptedLLM([submit(_summary_args())])
    summary = await write_summary(llm, PLAN, STEPS, NOTES)
    assert len(summary.sections) == 2
    assert summary.topic_id == "there_is_are" and summary.cefr == "A2"
    assert [str(s.url) for s in summary.sources] == [str(NOTES.source.url)]
    assert summary.provenance is not None and summary.provenance.component == "summary_writer"


async def test_write_summary_needs_one_section_per_step() -> None:
    args = _summary_args()
    short = {**args, "sections": args["sections"][:1]}
    llm = ScriptedLLM([submit(short), submit(args)])
    summary = await write_summary(llm, PLAN, STEPS, NOTES)
    assert len(summary.sections) == 2
    assert "one per step" in llm.calls[1]["messages"][-1]["content"]
