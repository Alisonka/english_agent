from __future__ import annotations

from typing import Any

import pytest

import english_agent.session_runner as runner
from conftest import topic_content_payload
from english_agent.llm import AssistantTurn, ToolCall, Usage
from english_agent.llm.agent_loop import BudgetExceeded
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.review import ReviewIssue, StepReview, SummaryReview
from english_agent.models.run import StageReport
from english_agent.models.topic import LessonStep, StepVariant, TopicContent, TopicPlan, TopicSummary
from english_agent.research.models import NOT_FOUND_MESSAGE, ResearchResult
from fakes import ScriptedLLM
from test_pipeline import make_settings
from test_topic_planner import NOTES

TOPIC = CurriculumTopic(
    id="there_is_are", title_en="there is / there are", title_ru="there is / there are", order=13, tags=["there"]
)
CONTENT = TopicContent.model_validate(topic_content_payload())
ACCEPT = StepReview(verdict="accept")
BLOCKING = ReviewIssue(where="message.key_idea_ru", kind="wrong_rule", blocking=True, fix="Fix the rule.")
REVISE = StepReview(verdict="revise", issues=[BLOCKING])


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


class Stages:
    """Stubbed stages: records what ran, and the Critic answers from `step_reviews` by (index, variant)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.feedback: list[tuple[str, str | None]] = []  # ("<index>:<variant>", feedback) per write
        self.step_reviews: dict[tuple[int, str], list[StepReview]] = {}


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> Stages:
    record = Stages()

    class FakeResearcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def run(self, topic: CurriculumTopic) -> ResearchResult:
            record.calls.append("research")
            return _found()

    async def fake_plan(llm: Any, topic: Any, notes: Any, **kwargs: Any) -> TopicPlan:
        record.calls.append("plan")
        return CONTENT.plan

    async def fake_write_step(
        llm: Any, plan: Any, index: int, notes: Any, *, variant: StepVariant = "main", **kwargs: Any
    ) -> LessonStep:
        record.calls.append(f"step:{index}:{variant}")
        record.feedback.append((f"{index}:{variant}", kwargs.get("feedback")))
        return CONTENT.step(index, variant)

    async def fake_write_summary(llm: Any, plan: Any, steps: Any, notes: Any, **kwargs: Any) -> TopicSummary:
        record.calls.append("summary")
        return CONTENT.summary

    async def fake_review_step(llm: Any, step: LessonStep, *args: Any, **kwargs: Any) -> StepReview:
        record.calls.append(f"review:{step.index}:{step.variant}")
        queued = record.step_reviews.get((step.index, step.variant))
        return queued.pop(0) if queued else ACCEPT

    async def fake_review_summary(llm: Any, *args: Any, **kwargs: Any) -> SummaryReview:
        record.calls.append("review:summary")
        return SummaryReview(verdict="accept")

    monkeypatch.setattr(runner, "ResearchAgent", FakeResearcher)
    monkeypatch.setattr(runner, "AnthropicLLM", lambda *a, **k: object())
    monkeypatch.setattr(runner, "plan_topic", fake_plan)
    monkeypatch.setattr(runner, "write_step", fake_write_step)
    monkeypatch.setattr(runner, "write_summary", fake_write_summary)
    monkeypatch.setattr(runner, "review_step", fake_review_step)
    monkeypatch.setattr(runner, "review_summary", fake_review_summary)
    return record


async def run(**settings: Any) -> runner.TopicSession:
    return await runner.run_topic_session(TOPIC, make_settings(**settings), client=object())  # type: ignore[arg-type]


async def test_without_the_critic_the_content_is_assembled_in_order(stages: Stages) -> None:
    session = await run(critic_model="")

    content = session.content
    assert content is not None
    assert content.item_keys() == ["there_is_are:singular", "there_is_are:plural"]
    assert [step.variant for step in content.alternatives] == ["alt", "alt"]
    assert all(step.review is None for step in content.steps)
    assert stages.calls == ["research", "plan", "step:1:main", "step:2:main", "step:1:alt", "step:2:alt", "summary"]
    report = session.report
    assert report.outcome == "ready" and report.critic_model is None
    assert [stage.name for stage in report.stages] == [
        "research",
        "plan",
        "step 1",
        "step 2",
        "step 1 alt",
        "step 2 alt",
        "summary",
    ]


async def test_every_step_and_the_cheat_sheet_are_reviewed_and_keep_their_review(stages: Stages) -> None:
    session = await run()

    content = session.content
    assert content is not None
    assert all(step.review is not None and step.review.verdict == "accept" for step in content.steps)
    assert all(step.review is not None for step in content.alternatives)
    assert content.summary.review is not None
    assert stages.calls.count("review:summary") == 1
    assert session.report.critic_model == "claude-sonnet-5-5"


async def test_a_rejected_step_is_rewritten_once_with_the_critics_feedback(stages: Stages) -> None:
    stages.step_reviews[(2, "main")] = [REVISE, ACCEPT]

    session = await run()

    assert session.content is not None
    first, rewrite = [feedback for key, feedback in stages.feedback if key == "2:main"]
    assert first is None
    assert rewrite is not None and "[MUST FIX] wrong_rule" in rewrite
    step_2 = next(stage for stage in session.report.stages if stage.name == "step 2")
    assert step_2.revisions == 1 and step_2.verdict == "accept" and step_2.blocking == []
    assert session.report.revisions == 1


async def test_a_step_still_rejected_after_its_rewrite_fails_the_whole_run(stages: Stages) -> None:
    stages.step_reviews[(1, "main")] = [REVISE, REVISE]

    session = await run()

    assert session.content is None
    report = session.report
    assert report.outcome == "failed"
    assert report.error is not None and "ReviewRejected" in report.error and "step 1" in report.error
    assert "wrong_rule @ message.key_idea_ru" in report.summary()
    assert "step:2:main" not in stages.calls  # nothing after the rejected step is written


async def test_a_rejected_alt_step_fails_the_run_with_a_readable_error(stages: Stages) -> None:
    stages.step_reviews[(2, "alt")] = [REVISE, REVISE]
    session = await run()
    assert session.content is None
    assert session.report.error is not None and session.report.error.startswith("ReviewRejected: step 2 alt")


async def test_research_without_notes_is_a_not_found_run(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(runner, "ResearchAgent", FakeResearcher)
    monkeypatch.setattr(runner, "AnthropicLLM", lambda *a, **k: object())
    session = await run()
    assert session.content is None
    assert session.report.outcome == "not_found" and session.report.error == NOT_FOUND_MESSAGE


def priced(cost: float) -> AssistantTurn:
    return AssistantTurn(content="x", tool_calls=[ToolCall(id="t", name="f", arguments={})], usage=Usage(cost_usd=cost))


async def test_the_meter_charges_every_call_and_stops_at_the_call_limit() -> None:
    meter = runner._Meter(max_calls=2, max_cost_usd=10.0)
    stage = StageReport(name="plan")
    llm = runner._MeteredLLM(ScriptedLLM([priced(0.25), priced(0.5), priced(1.0)]), meter, stage)

    await llm.chat([], [])
    await llm.chat([], [])
    with pytest.raises(BudgetExceeded):
        await llm.chat([], [])

    assert (meter.calls, stage.calls) == (2, 2)
    assert meter.cost_usd == pytest.approx(0.75) and stage.cost_usd == pytest.approx(0.75)
    assert llm.model == "claude-haiku-5-5"


async def test_the_meter_stops_once_the_cost_limit_is_spent() -> None:
    meter = runner._Meter(max_calls=10, max_cost_usd=0.5)
    llm = runner._MeteredLLM(ScriptedLLM([priced(0.6), priced(0.1)]), meter, StageReport(name="research"))
    await llm.chat([], [])
    with pytest.raises(BudgetExceeded, match=r"\$0.50"):
        await llm.chat([], [])
