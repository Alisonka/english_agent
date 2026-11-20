"""An exam on a block of studied topics: adaptive questions the learner has not seen, scored by code."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.practice import QuizQuestion
from english_agent.versions import Provenance

ExamState = Literal["running", "finished"]
ExamKind = Literal["exam", "placement"]
TopicVerdict = Literal["mastered", "shaky", "not_mastered"]
MASTERED_AT = 0.8
SHAKY_AT = 0.5


def verdict_for(correct: int, answers: int) -> TopicVerdict:
    """The code-owned verdict on one topic from its exam answers."""
    share = correct / answers if answers else 0.0
    return "mastered" if share >= MASTERED_AT else "shaky" if share >= SHAKY_AT else "not_mastered"


class PoolQuestion(BaseModel):
    """A question the exam may ask: from the topic's bank, never seen by the learner."""

    question_key: str
    topic_id: str
    item_id: int | None
    item_key: str
    difficulty: float
    question: QuizQuestion


class ExamAnswer(BaseModel):
    question_key: str
    topic_id: str
    correct: bool


class TopicResult(BaseModel):
    topic_id: str
    answers: int
    correct: int
    verdict: TopicVerdict  # code-owned
    recommendation_ru: str = Field(default="", max_length=200)


class ExamReportDraft(BaseModel):
    """What the Examiner model writes: a short summary and one recommendation per topic."""

    summary_ru: str = Field(min_length=10, max_length=500)
    recommendations: dict[str, str] = Field(default_factory=dict)  # topic id -> one short Russian line


class ExamReport(BaseModel):
    summary_ru: str
    topics: list[TopicResult]
    provenance: Provenance | None = None


class ExamRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    plan_date: date
    kind: ExamKind = "exam"
    state: ExamState
    topic_ids: list[str]
    ability_start: float
    ability: float
    pool: list[PoolQuestion]
    answers: list[ExamAnswer]
    report: ExamReport | None
    started_at: datetime
    finished_at: datetime | None
