"""run_topic_session: a curriculum topic in, the checked TopicContent and a report of the run out.

The one place that orchestrates content generation, and owns the rules that do not depend on a
model: the order of the stages, what the Critic may send back, how much a run may spend.

    research -> topic plan -> each main step: write -> Critic (-> one rewrite -> Critic)
             -> the re-teach (alt) steps and the cheat sheet, in parallel, each checked the same way

Every model call of the run goes through one meter, so `CONTENT_RUN_MAX_CALLS` and
`CONTENT_RUN_MAX_COST_USD` bound the whole run, research included. The run is fail-closed: a step
the Critic still rejects after the allowed rewrites, a spent budget or any error fails the whole
run (no partial content), and the caller records a failed content row that is retried later.
With `CRITIC_MODEL` empty the Critic is off and the steps are used as written.

The report (`RunReport`) is returned for ready and failed runs alike and stored with the content.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.critic import STEP_FIELDS, SUMMARY_FIELDS, review_step, review_summary, revision_feedback
from english_agent.llm import AnthropicLLM, AssistantTurn, ChatLLM, LLMError, LLMWriteError, make_client
from english_agent.metrics import CONTENT_RUNS, REVIEW_ISSUES, REVIEWS
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.review import StepReview, SummaryReview
from english_agent.models.run import RunOutcome, RunReport, StageReport
from english_agent.models.topic import LessonStep, StepVariant, TopicContent, TopicPlan, TopicSummary
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import TopicNotes
from english_agent.research.sitemaps import FileSitemapCache, SitemapCache, SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.step_writer import write_step
from english_agent.summary_writer import write_summary
from english_agent.topic_planner import plan_topic
from english_agent.tracing import get_tracer


class BudgetExceeded(LLMError):
    """The run reached its limit of model calls or of estimated cost."""


class ReviewRejected(LLMWriteError):
    """The Critic still found blocking issues after the allowed rewrites."""


@dataclass(frozen=True)
class TopicSession:
    """What a content run produced: the content (None when the run failed) and its report."""

    content: TopicContent | None
    report: RunReport


class _Meter:
    """The run's call and cost budget, shared by every stage (and the parallel ones)."""

    def __init__(self, max_calls: int, max_cost_usd: float) -> None:
        self.max_calls, self.max_cost_usd = max_calls, max_cost_usd
        self.calls, self.cost_usd = 0, 0.0

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"the run used its {self.max_calls} model calls")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"the run spent its ${self.max_cost_usd:.2f}")

    def record(self, stage: StageReport, turn: AssistantTurn) -> None:
        cost = turn.usage.cost_usd if turn.usage is not None and turn.usage.cost_usd is not None else 0.0
        self.calls += 1
        self.cost_usd += cost
        stage.calls += 1
        stage.cost_usd += cost


class _MeteredLLM:
    """A `ChatLLM` that charges every call to the run's meter and to one stage."""

    def __init__(self, inner: ChatLLM, meter: _Meter, stage: StageReport) -> None:
        self._inner, self._meter, self._stage = inner, meter, stage

    @property
    def model(self) -> str | None:
        return getattr(self._inner, "model", None)

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn:
        self._meter.check()
        turn = await self._inner.chat(messages, tools, tool_choice=tool_choice)
        self._meter.record(self._stage, turn)
        return turn


@contextmanager
def _timed(stage: StageReport) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        stage.seconds += time.monotonic() - started


def _research_http(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


def _first_error(error: BaseException) -> BaseException:
    """The first real error inside a task group's ExceptionGroup (or the error itself)."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _note_review(stage: StageReport, artifact: str, review: StepReview | SummaryReview) -> None:
    stage.verdict = review.verdict
    stage.blocking = [f"{issue.kind} @ {issue.where}" for issue in review.blocking]
    REVIEWS.labels(artifact, review.verdict).inc()
    for issue in review.issues:
        REVIEW_ISSUES.labels(issue.kind).inc()


@dataclass(frozen=True)
class _Stages:
    """How one run builds a stage's writer and Critic: the shared client, models and meter."""

    settings: Settings
    client: AsyncAnthropic
    meter: _Meter
    reports: list[StageReport]

    def stage(self, name: str) -> StageReport:
        report = StageReport(name=name)
        self.reports.append(report)
        return report

    def llm(self, model: str, stage: StageReport, *, effort: str | None = None) -> ChatLLM:
        return _MeteredLLM(
            AnthropicLLM(self.settings, model=model, effort=effort, client=self.client), self.meter, stage
        )

    def writer(self, stage: StageReport) -> ChatLLM:
        return self.llm(self.settings.writer_model, stage)

    def critic(self, stage: StageReport) -> ChatLLM | None:
        model = self.settings.critic_model
        return self.llm(model, stage, effort=self.settings.critic_effort) if model else None


async def _checked_step(
    stages: _Stages,
    stage: StageReport,
    plan: TopicPlan,
    index: int,
    notes: TopicNotes,
    *,
    variant: StepVariant,
    main: LessonStep | None,
    content_so_far: list[LessonStep],
    cefr: Cefr,
    curriculum_version: int | None,
) -> LessonStep:
    """Write a step and have the Critic check it, rewriting up to `CRITIC_MAX_REVISIONS` times."""
    writer, critic = stages.writer(stage), stages.critic(stage)
    feedback: str | None = None
    with _timed(stage):
        while True:
            step = await write_step(
                writer,
                plan,
                index,
                notes,
                variant=variant,
                main=main,
                content_so_far=content_so_far,
                cefr=cefr,
                curriculum_version=curriculum_version,
                feedback=feedback,
            )
            if critic is None:
                return step
            review = await review_step(critic, step, plan, notes, main=main, cefr=cefr)
            _note_review(stage, "step", review)
            if review.verdict == "accept":
                return step.model_copy(update={"review": review})
            if stage.revisions >= stages.settings.critic_max_revisions:
                raise ReviewRejected(f"{stage.name} still has blocking issues: {', '.join(stage.blocking)}")
            stage.revisions += 1
            feedback = revision_feedback(step, review.issues, include=STEP_FIELDS)


async def _checked_summary(
    stages: _Stages,
    stage: StageReport,
    plan: TopicPlan,
    steps: list[LessonStep],
    notes: TopicNotes,
    *,
    cefr: Cefr,
    curriculum_version: int | None,
) -> TopicSummary:
    """Write the cheat sheet and have the Critic check it, like a step."""
    writer, critic = stages.writer(stage), stages.critic(stage)
    feedback: str | None = None
    with _timed(stage):
        while True:
            summary = await write_summary(
                writer, plan, steps, notes, cefr=cefr, curriculum_version=curriculum_version, feedback=feedback
            )
            if critic is None:
                return summary
            review = await review_summary(critic, summary, steps, cefr=cefr)
            _note_review(stage, "summary", review)
            if review.verdict == "accept":
                return summary.model_copy(update={"review": review})
            if stage.revisions >= stages.settings.critic_max_revisions:
                raise ReviewRejected(f"{stage.name} still has blocking issues: {', '.join(stage.blocking)}")
            stage.revisions += 1
            feedback = revision_feedback(summary, review.issues, include=SUMMARY_FIELDS)


async def _generate(
    stages: _Stages,
    topic: CurriculumTopic,
    tools: LiveResearchTools,
    *,
    cefr: Cefr,
    curriculum_version: int | None,
) -> tuple[TopicContent | None, str]:
    """The content, or None and why (research found nothing). Raises on every other failure."""
    settings = stages.settings
    research_stage = stages.stage("research")
    with _timed(research_stage):
        researcher = ResearchAgent(
            stages.llm(settings.research_model, research_stage),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
            curriculum_version=curriculum_version,
            cefr=cefr,
        )
        research = await researcher.run(topic)
    if research.notes is None:
        return None, research.message
    notes = research.notes

    plan_stage = stages.stage("plan")
    with _timed(plan_stage):
        plan = await plan_topic(
            stages.writer(plan_stage), topic, notes, cefr=cefr, curriculum_version=curriculum_version
        )

    # Main steps in order: each one's brief recaps the steps before it.
    steps: list[LessonStep] = []
    for index in range(1, len(plan.steps) + 1):
        steps.append(
            await _checked_step(
                stages,
                stages.stage(f"step {index}"),
                plan,
                index,
                notes,
                variant="main",
                main=None,
                content_so_far=steps,
                cefr=cefr,
                curriculum_version=curriculum_version,
            )
        )

    # The re-teach steps and the cheat sheet depend only on the main steps: write them at once.
    alt_stages = [stages.stage(f"step {index} alt") for index in range(1, len(steps) + 1)]
    summary_stage = stages.stage("summary")
    async with asyncio.TaskGroup() as group:
        alt_tasks = [
            group.create_task(
                _checked_step(
                    stages,
                    alt_stages[index - 1],
                    plan,
                    index,
                    notes,
                    variant="alt",
                    main=steps[index - 1],
                    content_so_far=steps[: index - 1],
                    cefr=cefr,
                    curriculum_version=curriculum_version,
                )
            )
            for index in range(1, len(steps) + 1)
        ]
        summary_task = group.create_task(
            _checked_summary(
                stages, summary_stage, plan, steps, notes, cefr=cefr, curriculum_version=curriculum_version
            )
        )
    content = TopicContent(
        topic_id=topic.id,
        cefr=cefr,
        plan=plan,
        steps=steps,
        alternatives=[task.result() for task in alt_tasks],
        summary=summary_task.result(),
        sources=[notes.source],
    )
    return content, ""


async def run_topic_session(
    topic: CurriculumTopic,
    settings: Settings,
    *,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
    client: AsyncAnthropic | None = None,
    sitemaps: SitemapCache | None = None,
) -> TopicSession:
    """Write the topic's content. Never raises an `Exception`: a failed run is a session without content.

    `client` and `sitemaps` are the process's shared Claude client and sitemap cache; without them a
    client is opened (and closed) for this run and sitemaps are cached on disk.
    """
    started = time.monotonic()
    meter = _Meter(settings.content_run_max_calls, settings.content_run_max_cost_usd)
    reports: list[StageReport] = []
    content: TopicContent | None = None
    outcome: RunOutcome
    error: str | None
    with get_tracer().start_as_current_span("topic.run") as span:
        span.set_attribute("topic.id", topic.id)
        span.set_attribute("topic.cefr", cefr)
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(_research_http(settings))
                if client is None:
                    client = await stack.enter_async_context(make_client(settings))
                cache = sitemaps or FileSitemapCache(settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
                tools = LiveResearchTools(
                    http, SitemapIndex(http, cache), page_char_limit=settings.research_page_char_limit
                )
                stages = _Stages(settings, client, meter, reports)
                content, why_not = await _generate(
                    stages, topic, tools, cefr=cefr, curriculum_version=curriculum_version
                )
            outcome, error = ("ready", None) if content is not None else ("not_found", why_not)
        except Exception as raised:  # unattended run: every failure becomes a report, not a crash
            cause = _first_error(raised)
            outcome, error = "failed", f"{type(cause).__name__}: {cause}"
            span.record_exception(cause)
        report = RunReport(
            outcome=outcome,
            error=error,
            calls=meter.calls,
            cost_usd=round(meter.cost_usd, 6),
            seconds=round(time.monotonic() - started, 1),
            critic_model=settings.critic_model or None,
            stages=[stage.model_copy(update={"seconds": round(stage.seconds, 1)}) for stage in reports],
        )
        span.set_attribute("run.outcome", outcome)
        span.set_attribute("run.calls", report.calls)
        span.set_attribute("run.cost_usd", report.cost_usd)
        span.set_attribute("run.revisions", report.revisions)
        CONTENT_RUNS.labels(outcome).inc()
        return TopicSession(content, report)
