"""What the Critic says about a written step or cheat sheet, before any learner sees it.

The Critic is a second model that checks the writer's output against the research notes and the
level. It reports concrete issues; code decides the verdict (`revise` when any issue is
blocking), so a review can never say "accept" while listing a blocking problem. The Critic also
answers every quiz question on its own (`quiz_keys`, without seeing the writer's key): when its
answer differs from the stored key, code adds a blocking `wrong_key` issue.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from english_agent.versions import Provenance

MAX_REVIEW_ISSUES = 20

IssueKind = Literal[
    "wrong_rule",  # the explanation contradicts the notes or standard grammar
    "wrong_key",  # a quiz key is wrong (also added by code when the Critic's own answer differs)
    "ambiguous_options",  # more than one option is correct, or none is
    "bad_translation",  # an English sentence or its Russian is wrong or unnatural
    "off_level",  # vocabulary or grammar above the learner's level
    "off_step",  # teaches or tests material of another step
    "unclear_ru",  # the Russian explanation is confusing or wrong
    "reference_mismatch",  # a translation reference does not translate the Russian or lacks the step's form
    "mismatch_with_main",  # a re-teach step says something different from its main step
    "other",
]
Verdict = Literal["accept", "revise"]


class ReviewIssue(BaseModel):
    where: str = Field(min_length=1, max_length=80)  # the field, e.g. "practice.quiz[2].options"
    kind: IssueKind
    blocking: bool  # must be fixed before a learner sees it
    fix: str = Field(min_length=3, max_length=300)  # what to change, in English


def verdict_for(issues: list[ReviewIssue]) -> Verdict:
    """The verdict code assigns to a list of issues."""
    return "revise" if any(issue.blocking for issue in issues) else "accept"


class StepReview(BaseModel):
    """The Critic's review of one `LessonStep` (main or alt)."""

    verdict: Verdict  # code-owned: "revise" exactly when an issue is blocking
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=MAX_REVIEW_ISSUES)
    # The Critic's own answer (0-based option index) to every quiz question, in order; [] for an alt step.
    quiz_keys: list[int] = Field(default_factory=list, max_length=MAX_REVIEW_ISSUES)
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def consistent(self) -> "StepReview":
        if self.verdict != verdict_for(self.issues):
            raise ValueError("verdict must be 'revise' exactly when an issue is blocking")
        return self

    @property
    def blocking(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if issue.blocking]


class SummaryReview(BaseModel):
    """The Critic's review of a topic's cheat sheet (`TopicSummary`)."""

    verdict: Verdict  # code-owned, as in `StepReview`
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=MAX_REVIEW_ISSUES)
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def consistent(self) -> "SummaryReview":
        if self.verdict != verdict_for(self.issues):
            raise ValueError("verdict must be 'revise' exactly when an issue is blocking")
        return self

    @property
    def blocking(self) -> list[ReviewIssue]:
        return [issue for issue in self.issues if issue.blocking]
