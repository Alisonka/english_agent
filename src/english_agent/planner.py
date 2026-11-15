"""The Planner: what a day's practice reviews besides today's step, from the learner's own memory.

Pure functions over the learner's cards (`CardMemory`: FSRS due date and recall probability) and
review accuracy, so the rules are tested without a database. Today's step itself comes from
`progression.next_step` (including re-teaching); the Planner decides what is practised with it:

- quiz: 2 questions on today's step, 1 on the topic's least-recalled earlier step, and as many
  older-topic items as are due (at least 1, at most `MAX_REVIEW_SLOTS`), most overdue first, then
  the least recalled. A learner who keeps up gets a short quiz; one who fell behind catches up.
- translation: 2 sentences on today's step and 1 on what needs it most: an item missed in today's
  quiz, else an item due for review, else the topic's earlier step, else the least recalled.

The caller resolves each pick to a practice bank (an item taught long ago may have none) and takes
the first ones it can ask, so the ranked lists are longer than the slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from english_agent.models.storage import ItemAccuracy, PlanReason, PracticeSource

MAX_REVIEW_SLOTS = 3
WEAK_ACCURACY = 0.8  # below this (over at least 2 checks) an item counts as weak
WEAK_MIN_CHECKS = 2


@dataclass(frozen=True)
class Candidate:
    """An item the day's practice could review: an earlier step of today's topic, or an older item."""

    item_id: int
    item_key: str
    same_topic: bool  # an earlier step of the topic taught today
    due_at: datetime | None = None  # None: never reviewed yet (no card)
    retrievability: float = 0.0  # recall probability now; 0 for an item never reviewed
    accuracy: ItemAccuracy | None = None

    def is_due(self, now: datetime) -> bool:
        return self.due_at is not None and self.due_at <= now

    @property
    def is_weak(self) -> bool:
        accuracy = self.accuracy
        return (
            accuracy is not None
            and accuracy.checks >= WEAK_MIN_CHECKS
            and accuracy.ratio is not None
            and accuracy.ratio < WEAK_ACCURACY
        )


@dataclass(frozen=True)
class Pick:
    candidate: Candidate
    reason: PlanReason

    @property
    def source(self) -> PracticeSource:
        return "previous" if self.candidate.same_topic else "review"


@dataclass(frozen=True)
class QuizPlan:
    previous: Pick | None  # the topic's least-recalled earlier step; None on a topic's first step
    reviews: list[Pick]  # every older-topic item, best first; the caller asks the first `review_slots` it can
    review_slots: int


def _urgency(candidate: Candidate, now: datetime) -> tuple[int, float]:
    """Sort key: due items first, the most overdue first; then the least likely to be recalled."""
    if candidate.is_due(now):
        assert candidate.due_at is not None
        return 0, -(now - candidate.due_at).total_seconds()
    return 1, candidate.retrievability


def _reason(candidate: Candidate, now: datetime) -> PlanReason:
    if candidate.is_due(now):
        return "due"
    return "weak" if candidate.is_weak else "least_recalled"


def plan_quiz(candidates: list[Candidate], now: datetime) -> QuizPlan:
    """What the day's quiz reviews besides today's two questions."""
    earlier = [candidate for candidate in candidates if candidate.same_topic]
    previous = min(earlier, key=lambda candidate: candidate.retrievability) if earlier else None
    others = sorted((c for c in candidates if not c.same_topic), key=lambda c: _urgency(c, now))
    due = sum(1 for candidate in others if candidate.is_due(now))
    return QuizPlan(
        previous=Pick(previous, "previous") if previous is not None else None,
        reviews=[Pick(candidate, _reason(candidate, now)) for candidate in others],
        review_slots=min(len(others), max(1, min(due, MAX_REVIEW_SLOTS))),
    )


def plan_translation(candidates: list[Candidate], now: datetime, *, wrong_today: set[int]) -> list[Pick]:
    """The choices for the one sentence that is not today's, best first."""
    missed = sorted((c for c in candidates if c.item_id in wrong_today), key=lambda c: _urgency(c, now))
    rest = [candidate for candidate in candidates if candidate.item_id not in wrong_today]
    due = sorted((c for c in rest if not c.same_topic and c.is_due(now)), key=lambda c: _urgency(c, now))
    earlier = sorted((c for c in rest if c.same_topic), key=lambda c: c.retrievability)
    later = sorted((c for c in rest if not c.same_topic and not c.is_due(now)), key=lambda c: _urgency(c, now))
    return (
        [Pick(candidate, "wrong_today") for candidate in missed]
        + [Pick(candidate, "due") for candidate in due]
        + [Pick(candidate, "previous") for candidate in earlier]
        + [Pick(candidate, _reason(candidate, now)) for candidate in later]
    )
