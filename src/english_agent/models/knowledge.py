"""What the storage layer hands out about mastery estimates (see `english_agent.knowledge`)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LearnerAbility(BaseModel):
    """The learner's overall rating and the offsets of the tag groups they have answered in."""

    overall: float
    tags: dict[str, float] = Field(default_factory=dict)
    answers: int = 0  # answers behind the overall rating


class QuestionStat(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_key: str
    item_id: int | None
    topic_id: str | None
    kind: str
    difficulty: float
    prior: float
    prior_source: str
    answers: int
