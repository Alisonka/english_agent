"""The Mentor's memory: journal entries with checkable expectations, hypotheses, and the dossier.

The model writes the words (decision, reason, hypothesis, summary); code owns ids, dates, statuses
and every outcome: whether an expectation came true is computed from evidence, never judged.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from english_agent.versions import Provenance

ExpectationMetric = Literal["accuracy", "error_rate"]
ExpectationOutcome = Literal["met", "not_met", "insufficient"]
HypothesisStatus = Literal["open", "probing", "confirmed", "refuted", "stale"]
CLOSED_HYPOTHESES: frozenset[HypothesisStatus] = frozenset({"confirmed", "refuted", "stale"})


class Expectation(BaseModel):
    """What should be true about one topic or item by `due_on`, in a form code can check.

    `accuracy` is the share of correct answers; `error_rate` the share of answers with an error
    (of `error_type` only, when given). Fewer than `min_evidence` answers in the window is
    "insufficient", not a failure.
    """

    topic_id: str | None = Field(default=None, max_length=100)
    item_id: int | None = None
    metric: ExpectationMetric
    comparison: Literal["at_least", "at_most"]
    threshold: float = Field(ge=0, le=1)
    due_on: date
    error_type: str | None = Field(default=None, max_length=64)
    min_evidence: int = Field(default=3, ge=1, le=50)

    @model_validator(mode="after")
    def one_target(self) -> Expectation:
        if (self.topic_id is None) == (self.item_id is None):
            raise ValueError("an expectation is about exactly one of topic_id or item_id")
        if self.error_type is not None and self.metric != "error_rate":
            raise ValueError("error_type narrows error_rate only")
        return self


class JournalEntry(BaseModel):
    """One decision of the Mentor: what, why, on which evidence, and what it expects to happen."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    decision: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[int] = Field(min_length=1, max_length=20)
    expectation: Expectation | None
    outcome: ExpectationOutcome | None  # set by code once the expectation is due
    checked_at: datetime | None
    created_at: datetime


class Hypothesis(BaseModel):
    """A suspected cause of the learner's errors, tested over several days."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    statement_ru: str = Field(min_length=1, max_length=300)
    topic_id: str | None
    item_ids: list[int]
    evidence_ids: list[int] = Field(min_length=1)
    status: HypothesisStatus
    opened_at: datetime
    last_evidence_at: datetime  # the newest evidence that bore on it; staleness counts from here
    closed_at: datetime | None


class DossierDraft(BaseModel):
    """What the model writes when the journal is folded into a dossier."""

    summary_ru: str = Field(min_length=1, max_length=1500)  # what is known about this learner
    tried_ru: list[str] = Field(default_factory=list, max_length=10)  # interventions tried
    helped_ru: list[str] = Field(default_factory=list, max_length=10)  # the ones whose expectations were met


class Dossier(DossierDraft):
    model_config = ConfigDict(from_attributes=True)

    learner_id: int
    until_entry_id: int  # the newest journal entry it covers
    created_at: datetime
    provenance: Provenance
