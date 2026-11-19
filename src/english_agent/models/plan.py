"""The day plan: what one learner does tomorrow and why, as a document code can check and replay.

The Mentor proposes it, the policy layer checks it, the morning only executes it. When the Mentor
cannot plan (no new evidence, a failed session, exhausted limits), the deterministic policy that
ran the course before the agent writes it instead, marked `source="fallback"`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from english_agent.models.mentor import Expectation
from english_agent.models.topic import StepVariant
from english_agent.versions import Provenance

DayType = Literal[
    "new_topic",  # the first step of a topic from the frontier
    "continue_topic",  # the next step of the topic in progress
    "reteach",  # a step that did not stick, explained again (its alt variant)
    "remedial",  # a mini-lesson on a gap in a prerequisite
    "contrast",  # two constructions the learner confuses, side by side
    "review_day",  # reviews only, no new material
    "exam",  # an exam on a block of topics
    "light_day",  # less of everything: overloaded, or back after a pause
]
# Day types that send a step of a topic's content (the others are practice-only or exam days).
STEP_DAYS: frozenset[DayType] = frozenset({"new_topic", "continue_topic", "reteach", "remedial", "contrast"})
PlanSource = Literal["agent", "fallback"]
PlanState = Literal["pending", "running", "committed", "fallback"]


class PracticeDirective(BaseModel):
    """How the day's practice leans: focus items, probe questions, target difficulty, size."""

    focus_item_keys: list[str] = Field(default_factory=list, max_length=10)
    probe_question_keys: list[str] = Field(default_factory=list, max_length=5)
    target_success: float | None = Field(default=None, ge=0.3, le=0.95)  # predicted chance per question
    quiz_size: int | None = Field(default=None, ge=1, le=12)


class StepChoice(BaseModel):
    """The topic step a step day sends."""

    topic_id: str = Field(max_length=100)
    step_index: int = Field(ge=1)
    variant: StepVariant = "main"
    content_id: int | None = None  # the learner's pinned copy of the topic, once they started it
    leaving_topic_id: str | None = None  # the topic just finished: its cheat sheet goes out first


class FallbackBranch(BaseModel):
    """What the day becomes when the plan's content is not ready by the morning."""

    day_type: Literal["review_day", "continue_topic", "light_day"]
    step: StepChoice | None = None

    @model_validator(mode="after")
    def step_when_continuing(self) -> FallbackBranch:
        if (self.day_type == "continue_topic") != (self.step is not None):
            raise ValueError("a continue_topic fallback names its step, the others do not")
        return self


class DayPlan(BaseModel):
    learner_id: int
    plan_date: date
    day_type: DayType
    step: StepChoice | None = None
    exam_topic_ids: list[str] = Field(default_factory=list, max_length=10)
    practice: PracticeDirective = Field(default_factory=PracticeDirective)
    fallback: FallbackBranch | None = None
    explanation_ru: str = Field(default="", max_length=300)  # "what today and why", shown to the learner
    evidence_ids: list[int] = Field(default_factory=list, max_length=30)
    expectation: Expectation | None = None
    source: PlanSource
    evidence_until_id: int = 0  # the newest evidence the plan was made on: what was known at the time
    created_at: datetime
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def shape(self) -> DayPlan:
        if (self.day_type in STEP_DAYS) != (self.step is not None):
            raise ValueError("step days name their step, the other days do not")
        if (self.day_type == "exam") != bool(self.exam_topic_ids):
            raise ValueError("an exam day lists its topics, the other days do not")
        return self


class DayPlanRecord(BaseModel):
    """A row of `day_plans`: the plan, how it was made, and the Mentor's session trace."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    plan_date: date
    state: PlanState
    plan: DayPlan | None
    trajectory: list[dict[str, Any]] | None
    created_at: datetime
    committed_at: datetime | None
