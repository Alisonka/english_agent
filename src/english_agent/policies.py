"""The policy layer: rules every day plan must pass before it is committed, and the plan dry-run.

A violation is not a crash: it goes back to the Mentor as a tool result naming the rule and what is
wrong, so it can fix the plan and commit again. The fallback policy's plans pass every rule by
construction (tested). The dry-run shows a draft's consequences before it is committed: how many
questions, the predicted success, the day's load in minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from english_agent.models.plan import STEP_DAYS, DayPlan
from english_agent.topic_graph import TopicGraph

DEFAULT_QUIZ_SIZE = 5
MIN_QUIZ_WITH_DUE = 3  # due reviews may not be cut below this many questions
MAX_DAY_MINUTES = 25
MAX_DAY_MESSAGES = 10  # a full day: 5 polls, the summary, the step, a cheat sheet, the translation task
EXAM_GAP = timedelta(days=7)
EXAM_AFTER_RETURN = timedelta(days=3)
MAX_EXPLANATION = 300
# Load model for the dry-run: minutes per part of the day.
STEP_MINUTES = 5.0
QUESTION_MINUTES = 0.5
TRANSLATION_MINUTES = 6.0
EXAM_QUESTION_MINUTES = 1.0


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str

    def as_tool_result(self) -> dict[str, str]:
        return {"rule": self.rule, "problem": self.message}


@dataclass(frozen=True)
class PolicyContext:
    """What the rules need to know about the learner and the content, gathered by code."""

    today: date
    graph: TopicGraph
    known_topics: frozenset[str]
    studied_topics: frozenset[str]  # topics with at least one sent step
    due_reviews: int
    ready_topics: frozenset[str]  # topics whose content is written and checked
    invalid_evidence: tuple[int, ...] = ()  # cited ids that do not back a decision (from EvidenceRepo)
    last_exam_on: date | None = None
    returned_on: date | None = None  # the day the learner came back from a pause of 3+ days
    question_chances: tuple[float, ...] = field(default_factory=tuple)  # predicted P of the day's questions


def check_plan(plan: DayPlan, context: PolicyContext) -> list[Violation]:
    """Every rule the plan breaks; empty when it may be committed."""
    found: list[Violation] = []
    step = plan.step

    if plan.day_type == "new_topic" and step is not None:
        missing = [p for p in context.graph.prerequisites(step.topic_id) if p not in context.known_topics]
        if missing:
            found.append(
                Violation(
                    "prerequisites",
                    f"topic {step.topic_id} requires {', '.join(missing)}, not known yet; "
                    "plan a remedial on it or choose a frontier topic",
                )
            )
    revisits = plan.day_type in ("contrast", "reteach", "continue_topic")
    if revisits and step is not None and step.topic_id not in context.studied_topics:
        found.append(Violation("not_studied", f"{plan.day_type} needs a topic already studied; {step.topic_id} is not"))

    quiz_size = plan.practice.quiz_size or DEFAULT_QUIZ_SIZE
    if context.due_reviews > 0 and quiz_size < min(MIN_QUIZ_WITH_DUE, context.due_reviews):
        found.append(
            Violation(
                "reviews_cut", f"{context.due_reviews} reviews are due: the quiz needs at least {MIN_QUIZ_WITH_DUE}"
            )
        )
    run = dry_run(plan, context)
    if run.minutes > MAX_DAY_MINUTES:
        found.append(Violation("load", f"the day takes about {run.minutes:.0f} min, more than {MAX_DAY_MINUTES}"))
    if run.messages > MAX_DAY_MESSAGES:
        found.append(Violation("load", f"the day sends {run.messages} messages, more than {MAX_DAY_MESSAGES}"))

    if plan.day_type == "exam":
        if context.last_exam_on is not None and context.today - context.last_exam_on < EXAM_GAP:
            found.append(Violation("exam_gap", f"the last exam was on {context.last_exam_on}; one a week at most"))
        if context.returned_on is not None and context.today - context.returned_on < EXAM_AFTER_RETURN:
            found.append(Violation("exam_after_pause", "no exam in the first days after coming back from a pause"))
        unstudied = [topic for topic in plan.exam_topic_ids if topic not in context.studied_topics]
        if unstudied:
            found.append(
                Violation("exam_topics", f"an exam only covers studied topics; not studied: {', '.join(unstudied)}")
            )

    if plan.source == "agent":
        if not plan.evidence_ids:
            found.append(Violation("evidence", "a decision must cite the evidence it is based on"))
        elif context.invalid_evidence:
            ids = ", ".join(str(i) for i in context.invalid_evidence)
            found.append(Violation("evidence", f"cited evidence does not exist or is not this learner's: {ids}"))
        if not plan.explanation_ru.strip():
            found.append(Violation("explanation", "say in one or two sentences what today is and why"))

    needs_content = step is not None and step.content_id is None and step.topic_id not in context.ready_topics
    if step is not None and plan.day_type in STEP_DAYS and needs_content and plan.fallback is None:
        found.append(
            Violation(
                "fallback",
                f"the content of {step.topic_id} is not ready: add a fallback branch that needs no new content",
            )
        )
    branch = plan.fallback.step if plan.fallback is not None else None
    if branch is not None and branch.content_id is None and branch.topic_id not in context.ready_topics:
        found.append(Violation("fallback", "the fallback branch itself needs content that is not ready"))
    if len(plan.explanation_ru) > MAX_EXPLANATION:
        found.append(Violation("explanation", f"the explanation is longer than {MAX_EXPLANATION} characters"))
    return found


@dataclass(frozen=True)
class DryRun:
    questions: int
    predicted_success: float | None  # mean predicted chance on the day's questions; None when unknown
    minutes: float
    messages: int

    def as_tool_result(self, violations: list[Violation]) -> dict[str, object]:
        return {
            "questions": self.questions,
            "predicted_success": round(self.predicted_success, 2) if self.predicted_success is not None else None,
            "minutes": round(self.minutes, 1),
            "messages": self.messages,
            "violations": [violation.as_tool_result() for violation in violations],
        }


def dry_run(plan: DayPlan, context: PolicyContext) -> DryRun:
    """What the draft would make of the day: questions, predicted success, load."""
    if plan.day_type == "exam":
        questions = 4 * len(plan.exam_topic_ids)
        return DryRun(questions, _mean(context.question_chances), questions * EXAM_QUESTION_MINUTES, questions + 1)
    questions = plan.practice.quiz_size or DEFAULT_QUIZ_SIZE
    if plan.day_type == "light_day":
        questions = min(questions, 3)
    minutes = questions * QUESTION_MINUTES
    messages = questions + 1  # the quiz polls and the summary
    if plan.step is not None:
        minutes += STEP_MINUTES
        messages += 1 + (1 if plan.step.leaving_topic_id else 0)
    if plan.day_type != "light_day":
        minutes += TRANSLATION_MINUTES
        messages += 1
    return DryRun(questions, _mean(context.question_chances), minutes, messages)


def _mean(values: tuple[float, ...]) -> float | None:
    return sum(values) / len(values) if values else None
