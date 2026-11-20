"""Exercises the Mentor orders for one learner's gap: probes, contrast pairs and drills.

A probe tells two of the Diagnostician's hypotheses apart; a contrast pair puts two confused
constructions side by side; a drill gives a weak item more practice, on a theme the learner likes
when asked. Every exercise is answered independently by the Critic before anyone sees it. Exercises
without a personal theme join the item's shared bank and are calibrated on every learner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.practice import QuizQuestion

ExerciseKind = Literal["probe", "contrast_pair", "drill"]


class ExerciseOrder(BaseModel):
    kind: ExerciseKind
    item_key: str = Field(min_length=3, max_length=200)
    count: int = Field(default=2, ge=1, le=5)
    error_type: str | None = Field(default=None, max_length=32)
    hypothesis_ru: str | None = Field(default=None, max_length=300)  # what a probe must tell apart
    theme: str | None = Field(default=None, max_length=30)  # one of the learner's interests: personal
    target_difficulty: int = Field(default=3, ge=1, le=5)  # 1 very easy .. 5 very hard for the level


class ComposedExercises(BaseModel):
    """What the writer returns."""

    questions: list[QuizQuestion] = Field(min_length=1, max_length=5)


class IndependentAnswers(BaseModel):
    """What the Critic returns, without having seen the keys."""

    keys: list[int] = Field(max_length=5)
    difficulty: list[Annotated[int, Field(ge=1, le=5)]] = Field(max_length=5)


class BankExercise(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_key: str
    item_id: int | None
    item_key: str
    topic_id: str
    kind: ExerciseKind
    question: QuizQuestion
    learner_id: int | None  # set for a personal exercise (a theme); None: in the item's shared bank
    created_at: datetime
