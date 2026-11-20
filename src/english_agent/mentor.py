"""The Mentor: an orchestrator agent that decides what one learner does tomorrow, and why.

A session runs in the evening (and on events) when the learner has new evidence; without any, code
plans the day and no model is called. The loop is reason -> tool -> result: the model starts from a
short brief and fetches everything else through tools (independent reads run in parallel). It may
delegate to the Diagnostician, write its journal and hypotheses, try a draft plan against the policy
layer (`dry_run_plan`) and finally `commit_plan`. A plan the policy layer refuses comes back as a tool
result naming the broken rule, and the Mentor fixes it.

Limits: `mentor_max_steps` orchestrator steps, `mentor_max_subagents` subagent calls, one budget of
model calls, cost and time shared with the subagents. Running out, a failed API, or no committed
plan means the deterministic policy plans the day instead: the learner always gets a lesson, and
the share of fallback plans is a health metric. In shadow mode the Mentor's plan is kept in the
session trace beside the fallback plan that actually runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from opentelemetry.trace import Span
from pydantic import ValidationError

from english_agent.composer import compose_exercises
from english_agent.content import content_key
from english_agent.day_plan import frontier_known
from english_agent.diagnostician import diagnose
from english_agent.examiner import exam_pool
from english_agent.llm import ChatLLM, LLMError, LLMWriteError, ToolCall, tool_schema
from english_agent.llm.agent_loop import Budget, Finish, MeteredLLM, ToolError, run_subagent, run_tool_loop
from english_agent.mentor_memory import write_dossier
from english_agent.metrics import MENTOR_COST, MENTOR_SESSIONS, MENTOR_STEPS, POLICY_REFUSALS
from english_agent.models.content_order import ContentBrief
from english_agent.models.curriculum import all_topics, load_curriculum
from english_agent.models.exercise import ExerciseOrder
from english_agent.models.mentor import Expectation, HypothesisStatus
from english_agent.models.plan import DayPlan
from english_agent.models.storage import LearnerRecord
from english_agent.pipeline import Runtime, fallback_for_day
from english_agent.policies import PolicyContext, check_plan, dry_run
from english_agent.progression import topic_progress
from english_agent.storage import (
    EvidenceRepo,
    InvalidCitationsError,
    KnowledgeRepo,
    MentorMemoryRepo,
    PlanRepo,
)
from english_agent.topic_graph import default_graph
from english_agent.tracing import get_tracer
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

log = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "mentor_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("learner_id", "plan_date", "source", "created_at", "evidence_until_id", "provenance")
QUIET_SINCE = timedelta(days=2)  # with no plan yesterday, evidence this recent counts as new
DOSSIER_EVERY = timedelta(days=7)
DOSSIER_MIN_ENTRIES = 5
MAX_FRESH = 40  # evidence rows per fresh_evidence call
MAX_RESULT_CHARS = 600  # per tool result in the stored trajectory

MentorOutcome = Literal["off", "already_planned", "no_new_evidence", "committed", "shadow", "fallback"]


@dataclass(frozen=True)
class MentorRepos:
    """The repositories a session reads and writes beyond the runtime's stores."""

    memory: MentorMemoryRepo
    knowledge: KnowledgeRepo
    evidence: EvidenceRepo
    plans: PlanRepo


async def run_mentor_session(
    runtime: Runtime,
    repos: MentorRepos,
    learner: LearnerRecord,
    day: date,
    now: datetime,
    *,
    llm: ChatLLM,
    diagnostician_llm: ChatLLM | None = None,
    composer_llms: tuple[ChatLLM, ChatLLM] | None = None,  # (writer, Critic) for order_exercises
    trigger: str = "evening",
) -> MentorOutcome:
    """Plan `day` for the learner; every path ends with a committed plan unless the course is over."""
    settings = runtime.settings
    if settings.mentor_mode == "off":
        return "off"
    if not await repos.plans.start(learner.id, day):
        return "already_planned"
    with get_tracer().start_as_current_span("mentor.session") as span:
        span.set_attribute("mentor.trigger", trigger)
        await repos.memory.check_due_expectations(now.date(), now)
        await repos.memory.close_stale(now)

        yesterday = await runtime.stores.plans.get(learner.id, day - timedelta(days=1))
        since = yesterday.committed_at if yesterday is not None and yesterday.committed_at else now - QUIET_SINCE
        fresh = await repos.evidence.since(learner.id, since)
        fallback = await fallback_for_day(runtime, learner, day, now)
        if not fresh:
            return await _finish(runtime, fallback, None, "no_new_evidence", now)

        budget = Budget(
            settings.mentor_max_calls,
            settings.mentor_max_cost_usd,
            max_subagent_calls=settings.mentor_max_subagents,
            timeout_s=settings.mentor_timeout_seconds,
            subject="the session",
        )
        session = _Session(runtime, repos, learner, day, now, budget, max(e.id for e in fresh), trigger)
        plan: DayPlan | None = None
        try:
            async with asyncio.timeout(settings.mentor_timeout_seconds):
                await session.maybe_fold_dossier(MeteredLLM(llm, budget))
                if composer_llms is not None:
                    session.composer = (MeteredLLM(composer_llms[0], budget), MeteredLLM(composer_llms[1], budget))
                plan = await session.run(MeteredLLM(llm, budget), MeteredLLM(diagnostician_llm or llm, budget))
        except (LLMError, LLMWriteError, TimeoutError) as error:
            log.warning("mentor session for learner %s failed: %s: %s", learner.id, type(error).__name__, error)
            session.trajectory.append({"error": f"{type(error).__name__}: {error}"[:MAX_RESULT_CHARS]})
        MENTOR_STEPS.observe(session.steps)
        MENTOR_COST.observe(budget.cost_usd)
        span.set_attribute("mentor.steps", session.steps)
        span.set_attribute("mentor.cost_usd", budget.cost_usd)
        if plan is None:
            return await _finish(runtime, fallback, session.trajectory, "fallback", now)
        if settings.mentor_mode == "shadow":
            shadow = [*session.trajectory, {"shadow_plan": plan.model_dump(mode="json")}]
            return await _finish(runtime, fallback, shadow, "shadow", now)
        await runtime.stores.plans.commit(plan, trajectory=session.trajectory, now=now)
        MENTOR_SESSIONS.labels("committed").inc()
        return "committed"


async def _finish(
    runtime: Runtime,
    fallback: DayPlan | None,
    trajectory: list[dict[str, Any]] | None,
    outcome: MentorOutcome,
    now: datetime,
) -> MentorOutcome:
    if fallback is not None:
        await runtime.stores.plans.commit(fallback, trajectory=trajectory, now=now)
    MENTOR_SESSIONS.labels(outcome).inc()
    return outcome


class _Session:
    """One session's state: the learner, the budget, the trace, and the tools."""

    def __init__(
        self,
        runtime: Runtime,
        repos: MentorRepos,
        learner: LearnerRecord,
        day: date,
        now: datetime,
        budget: Budget,
        evidence_until_id: int,
        trigger: str,
    ) -> None:
        self.runtime, self.repos, self.learner, self.day, self.now = runtime, repos, learner, day, now
        self.budget, self.evidence_until_id, self.trigger = budget, evidence_until_id, trigger
        self.trajectory: list[dict[str, Any]] = []
        self.steps = 0
        self._diagnostician: ChatLLM | None = None
        self.composer: tuple[ChatLLM, ChatLLM] | None = None

    # -- the loop --------------------------------------------------------------------

    async def run(self, llm: ChatLLM, diagnostician: ChatLLM) -> DayPlan | None:
        self._diagnostician = diagnostician
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": self._brief()},
        ]
        plan: DayPlan | None
        plan, self.steps = await run_tool_loop(
            llm,
            messages,
            _tools(),
            self._traced,
            max_steps=self.runtime.settings.mentor_max_steps,
            nudge="Use the tools; finish with commit_plan.",
            step_attribute="mentor.step",
            parallel=True,
        )
        return plan

    def _brief(self) -> str:
        learner = self.learner
        return (
            f"Learner {learner.id}, level {learner.cefr}, goal {learner.goal or 'not set'}, "
            f"interests {', '.join(learner.interests) or 'not set'}. Plan the day {self.day.isoformat()} "
            f"(today is {self.now.date().isoformat()}). Session started by: {self.trigger}."
        )

    async def _traced(self, call: ToolCall, step: int, span: Span) -> Any:
        entry: dict[str, Any] = {"step": step, "tool": call.name, "arguments": call.arguments}
        try:
            result = await self._handle(call)
        except ToolError as error:
            entry.update(ok=False, result=str(error)[:MAX_RESULT_CHARS])
            self.trajectory.append(entry)
            raise
        shown = result.value.model_dump(mode="json") if isinstance(result, Finish) else result
        entry.update(ok=True, result=json.dumps(shown, ensure_ascii=False, default=str)[:MAX_RESULT_CHARS])
        self.trajectory.append(entry)
        return result

    async def _handle(self, call: ToolCall) -> Any:
        arguments = call.arguments
        match call.name:
            case "learner_snapshot":
                return await self._snapshot()
            case "skill_map":
                return await self._skill_map([str(topic) for topic in arguments.get("topic_ids") or []])
            case "fresh_evidence":
                return await self._fresh(arguments.get("topic_id"))
            case "due_reviews":
                due = await self.runtime.stores.cards.due(self.learner.id, self.now)
                return {"due": len(due)}
            case "content_status":
                return await self._content_status(str(arguments.get("topic_id", "")))
            case "dry_run_plan":
                plan = self._plan_from(arguments.get("plan"))
                context = await self._policy_context(plan)
                return dry_run(plan, context).as_tool_result(check_plan(plan, context))
            case "diagnose":
                return await self._diagnose(str(arguments.get("question") or ""))
            case "order_content":
                return await self._order_content(arguments)
            case "order_exercises":
                return await self._order_exercises(arguments)
            case "prepare_exam":
                return await self._prepare_exam([str(topic) for topic in arguments.get("topic_ids") or []])
            case "journal_note":
                return await self._journal(arguments)
            case "hypothesis":
                return await self._hypothesis(arguments)
            case "commit_plan":
                plan = self._plan_from(arguments.get("plan"))
                violations = check_plan(plan, await self._policy_context(plan))
                if violations:
                    for violation in violations:
                        POLICY_REFUSALS.labels(violation.rule).inc()
                    refused = json.dumps([v.as_tool_result() for v in violations], ensure_ascii=False)
                    raise ToolError(f"the plan breaks these rules, fix it and commit again: {refused}")
                return Finish(plan)
        raise ToolError(f"unknown tool {call.name!r}")

    # -- reads -----------------------------------------------------------------------

    async def _snapshot(self) -> dict[str, Any]:
        learner, repos, stores = self.learner, self.repos, self.runtime.stores
        ability = await repos.knowledge.ability(learner.id)
        statuses = await repos.knowledge.topic_statuses(learner.id, self.now)
        frontier = await self._frontier()
        entries = await repos.memory.entries(learner.id, limit=8)
        dossier = await repos.memory.latest_dossier(learner.id)
        hypotheses = await repos.memory.open_hypotheses(learner.id)
        due = await stores.cards.due(learner.id, self.now)
        sent = await stores.lessons.sent_steps(learner.id, learner.cefr)
        current = sent[-1] if sent else None
        return {
            "level": learner.cefr,
            "ability": None if ability is None else {"overall": round(ability.overall, 2), "answers": ability.answers},
            "current_step": None
            if current is None
            else {"topic": current.topic_id, "step": current.step_index, "variant": current.variant},
            "topics_in_progress": {t: s for t, s in statuses.items() if s in ("learning", "shaky")},
            "frontier": frontier[:8],
            "due_reviews": len(due),
            "open_hypotheses": [
                {"id": h.id, "statement": h.statement_ru, "status": h.status, "evidence": h.evidence_ids}
                for h in hypotheses
            ],
            "journal": [
                {"decision": e.decision, "outcome": e.outcome, "at": f"{e.created_at:%Y-%m-%d}"} for e in entries
            ],
            "dossier": None if dossier is None else dossier.summary_ru,
        }

    async def _frontier(self) -> list[str]:
        learner = self.learner
        sent = await self.runtime.stores.lessons.sent_steps(learner.id, learner.cefr)
        known = await self.repos.knowledge.known_topics(learner.id, self.now)
        graph = default_graph()
        return graph.frontier(frontier_known(graph, learner.cefr, topic_progress(sent), known), cefr=learner.cefr)

    async def _skill_map(self, topic_ids: list[str]) -> dict[str, Any]:
        graph = default_graph()
        unknown = [topic for topic in topic_ids if topic not in graph.nodes]
        if unknown or not topic_ids:
            raise ToolError(f"give known topic_ids; unknown: {unknown}")
        statuses = await self.repos.knowledge.topic_statuses(self.learner.id, self.now)
        return {
            topic: {
                "status": statuses.get(topic, "not_open"),
                "prerequisites": {p: statuses.get(p, "not_open") for p in graph.prerequisites(topic)},
            }
            for topic in topic_ids[:10]
        }

    async def _fresh(self, topic_id: object) -> list[dict[str, Any]]:
        since = self.now - QUIET_SINCE
        rows = await self.repos.evidence.since(self.learner.id, since, topic_id=str(topic_id) if topic_id else None)
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "topic": row.topic_id,
                "at": f"{row.occurred_at:%Y-%m-%d %H:%M}",
                "outcome": row.payload.get("verdict", row.payload.get("is_correct")),
                "cancelled": row.cancelled_at is not None,
            }
            for row in rows[-MAX_FRESH:]
        ]

    async def _content_status(self, topic_id: str) -> dict[str, str]:
        settings, level = self.runtime.settings, self.learner.cefr
        curriculum = load_curriculum(level)
        ready = await self.runtime.stores.contents.ready_topics(level, curriculum.version, content_key(settings))
        failed = await self.runtime.stores.contents.unavailable_topics(
            level, curriculum.version, content_key(settings), max_attempts=settings.job_max_attempts
        )
        status = "ready" if topic_id in ready else "failed" if topic_id in failed else "missing"
        return {"topic": topic_id, "status": status}

    async def _policy_context(self, plan: DayPlan) -> PolicyContext:
        learner, stores, settings = self.learner, self.runtime.stores, self.runtime.settings
        sent = await stores.lessons.sent_steps(learner.id, learner.cefr)
        graph = default_graph()
        known = await self.repos.knowledge.known_topics(learner.id, self.now)
        curriculum = load_curriculum(learner.cefr)
        ready = await stores.contents.ready_topics(learner.cefr, curriculum.version, content_key(settings))
        invalid = await self.repos.evidence.invalid_citations(learner.id, plan.evidence_ids)
        return PolicyContext(
            today=self.day,
            graph=graph,
            known_topics=frozenset(frontier_known(graph, learner.cefr, topic_progress(sent), known)),
            studied_topics=frozenset(step.topic_id for step in sent),
            due_reviews=len(await stores.cards.due(learner.id, self.now)),
            ready_topics=frozenset(ready),
            invalid_evidence=tuple(invalid),
            last_exam_on=await stores.exams.last_exam_on(learner.id),
        )

    # -- delegation and writes -------------------------------------------------------------

    async def _diagnose(self, question: str) -> dict[str, Any]:
        llm = self._diagnostician
        assert llm is not None
        learner, now = self.learner, self.now
        inputs = await self.runtime.stores.profiles.inputs(learner.id, now, window_days=14)
        accuracy = await self.runtime.stores.cards.item_accuracy(learner.id, list(inputs.items))

        async def run() -> dict[str, Any]:
            found = await diagnose(
                llm,
                learner_id=learner.id,
                inputs=inputs,
                accuracy=accuracy,
                previous=inputs.reflection,
                now=now,
                reader=self.repos.evidence,
                graph=default_graph(),
                question=question or None,
            )
            return found.model_dump(mode="json", include={"focus_points", "notes_ru"})

        return await run_subagent(self.budget, "diagnostician", run)

    async def _order_content(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Order a remedial or contrast lesson; returns a receipt (it is written in the background)."""
        try:
            brief = ContentBrief.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(f"the brief is not valid: {error.errors(include_url=False)[:3]}") from error
        unknown = [topic for topic in brief.topic_ids if topic not in all_topics()]
        if unknown:
            raise ToolError(f"unknown topics: {unknown}")
        order = await self.runtime.stores.orders.order(brief, self.learner.cefr)
        return {"order_id": order.id, "topic_id": order.topic_id, "status": order.status, "steps": brief.steps}

    async def _order_exercises(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Compose probes, contrast pairs or drills now (a subagent call); returns their question keys."""
        if self.composer is None:
            raise ToolError("order_exercises is not available in this run")
        try:
            order = ExerciseOrder.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(f"the order is not valid: {error.errors(include_url=False)[:3]}") from error
        if order.theme is not None and order.theme not in self.learner.interests:
            raise ToolError(f"theme must be one of the learner's interests: {self.learner.interests}")
        item = await self.runtime.stores.items.get_by_key(order.item_key)
        if item is None:
            raise ToolError(f"unknown item_key {order.item_key!r}")
        writer, critic = self.composer

        async def run() -> dict[str, Any]:
            exercises, priors = await compose_exercises(
                writer, critic, order, item, cefr=self.learner.cefr, learner_id=self.learner.id, now=self.now
            )
            await self.runtime.stores.exercises.save(exercises, priors)
            return {"question_keys": [e.question_key for e in exercises], "kept": len(exercises), "asked": order.count}

        return await run_subagent(self.budget, "exercise_composer", run)

    async def _prepare_exam(self, topic_ids: list[str]) -> dict[str, Any]:
        """How many unseen questions each topic has for an exam (code runs and scores the exam itself)."""
        if not topic_ids:
            raise ToolError("give the topic_ids of the block")
        pool = await exam_pool(self.runtime.stores, self.learner, topic_ids)
        counts = {topic: sum(1 for q in pool if q.topic_id == topic) for topic in topic_ids}
        return {"unseen_questions": counts, "ready": all(count >= 2 for count in counts.values())}

    async def _journal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            expectation = Expectation.model_validate(arguments["expectation"]) if arguments.get("expectation") else None
            entry = await self.repos.memory.add_entry(
                self.learner.id,
                decision=str(arguments.get("decision", ""))[:300],
                reason=str(arguments.get("reason", ""))[:500],
                evidence_ids=[int(i) for i in arguments.get("evidence_ids") or []],
                expectation=expectation,
            )
        except (ValidationError, InvalidCitationsError, ValueError) as error:
            raise ToolError(f"journal entry refused: {error}") from error
        return {"entry_id": entry.id}

    async def _hypothesis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        evidence = [int(i) for i in arguments.get("evidence_ids") or []]
        try:
            if arguments.get("action") == "open":
                opened = await self.repos.memory.open_hypothesis(
                    self.learner.id,
                    statement_ru=str(arguments.get("statement_ru", ""))[:300],
                    evidence_ids=evidence,
                    topic_id=arguments.get("topic_id"),
                    now=self.now,
                )
                return {"hypothesis_id": opened.id, "status": opened.status}
            status: HypothesisStatus = arguments.get("status", "")
            moved = await self.repos.memory.move_hypothesis(
                self.learner.id, int(arguments.get("hypothesis_id", 0)), status, evidence_ids=evidence, now=self.now
            )
            return {"hypothesis_id": moved.id, "status": moved.status}
        except (InvalidCitationsError, ValueError) as error:
            raise ToolError(f"hypothesis refused: {error}") from error

    def _plan_from(self, arguments: object) -> DayPlan:
        if not isinstance(arguments, dict):
            raise ToolError("pass the plan as an object")
        try:
            return DayPlan.model_validate(
                {
                    **arguments,
                    "learner_id": self.learner.id,
                    "plan_date": self.day,
                    "source": "agent",
                    "created_at": self.now,
                    "evidence_until_id": self.evidence_until_id,
                    "provenance": build_provenance(component="mentor", schema_version=SCHEMA_VERSIONS["day_plan"]),
                }
            )
        except ValidationError as error:
            problems = json.dumps(error.errors(include_url=False)[:5], ensure_ascii=False, default=str)
            raise ToolError(f"the plan is not valid: {problems}") from error

    async def maybe_fold_dossier(self, llm: ChatLLM) -> None:
        """Once a week, fold the journal into the dossier (one model call, on the session's budget)."""
        memory, learner = self.repos.memory, self.learner
        previous = await memory.latest_dossier(learner.id)
        if previous is not None and self.now - previous.created_at < DOSSIER_EVERY:
            return
        entries = await memory.entries(learner.id, after_id=previous.until_entry_id if previous else 0)
        if len(entries) < DOSSIER_MIN_ENTRIES:
            return
        try:
            await memory.save_dossier(await write_dossier(llm, learner.id, entries, previous, self.now))
        except (LLMError, LLMWriteError) as error:
            log.warning("dossier for learner %s not written: %s", learner.id, error)


def _tools() -> list[dict[str, Any]]:
    def tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        parameters = {"type": "object", "properties": properties, "required": required}
        return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}

    plan = tool_schema(DayPlan, name="plan", description="", owned=_OWNED_BY_CODE)["function"]["parameters"]
    evidence = {"type": "array", "items": {"type": "integer"}, "description": "ids from fresh_evidence"}
    expectation = Expectation.model_json_schema()
    topic = {"type": "string"}
    return [
        tool("learner_snapshot", "Level, ability, current step, frontier, due reviews, hypotheses, journal.", {}, []),
        tool(
            "skill_map",
            "Status of topics and of their prerequisites.",
            {"topic_ids": {"type": "array", "items": topic}},
            ["topic_ids"],
        ),
        tool(
            "fresh_evidence",
            "The learner's latest answers and events, optionally for one topic.",
            {"topic_id": topic},
            [],
        ),
        tool("due_reviews", "How many reviews are due, counted by code.", {}, []),
        tool(
            "content_status",
            "Whether a topic's content is ready, failed or missing.",
            {"topic_id": topic},
            ["topic_id"],
        ),
        tool(
            "dry_run_plan",
            "What a draft plan would make of the day, and which rules it breaks.",
            {"plan": plan},
            ["plan"],
        ),
        tool(
            "diagnose",
            "Ask the Diagnostician why the learner errs (a subagent call).",
            {"question": {"type": "string"}},
            ["question"],
        ),
        tool(
            "order_content",
            "Order a remedial (one topic and the error type) or a contrast (two topics) lesson. Returns a receipt "
            "with the topic_id to plan; it is written in the background, so give the plan a fallback branch.",
            ContentBrief.model_json_schema()["properties"],
            ["kind", "topic_ids", "focus_ru"],
        ),
        tool(
            "order_exercises",
            "Compose probes (tell a hypothesis apart), contrast pairs or drills for one item; returns question keys "
            "to put in the plan's practice.probe_question_keys. Counts as a subagent call.",
            ExerciseOrder.model_json_schema()["properties"],
            ["kind", "item_key"],
        ),
        tool(
            "prepare_exam",
            "Prepare an exam on a block of topics.",
            {"topic_ids": {"type": "array", "items": topic}},
            ["topic_ids"],
        ),
        tool(
            "journal_note",
            "Record a decision, its reason, its evidence and a checkable expectation.",
            {
                "decision": {"type": "string"},
                "reason": {"type": "string"},
                "evidence_ids": evidence,
                "expectation": expectation,
            },
            ["decision", "reason", "evidence_ids"],
        ),
        tool(
            "hypothesis",
            "Open a hypothesis, or move one to probing, confirmed or refuted.",
            {
                "action": {"type": "string", "enum": ["open", "move"]},
                "statement_ru": {"type": "string"},
                "topic_id": topic,
                "hypothesis_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["probing", "confirmed", "refuted"]},
                "evidence_ids": evidence,
            },
            ["action", "evidence_ids"],
        ),
        tool(
            "commit_plan",
            "Commit the day plan. Ends the session once the policy layer accepts it.",
            {"plan": plan},
            ["plan"],
        ),
    ]
