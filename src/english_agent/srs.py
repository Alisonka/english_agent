"""Spaced repetition: a thin wrapper over py-fsrs (FSRS-6) so no other module sees its API.

A card is one learner's memory of one item; its state is `Card.to_dict()` JSON in
`cards.fsrs_state`. Grammar items get at most one rating a day, so the flashcard learning
steps (1 min, 10 min) are turned off: a single rating moves the card straight into day-scale
review intervals. py-fsrs works in UTC only, hence the aware-datetime checks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fsrs import Card, Rating, Scheduler

from english_agent.config import Settings
from english_agent.models.practice import TranslationVerdict

AGAIN, GOOD = int(Rating.Again), int(Rating.Good)


def utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("review time must be timezone-aware")
    return at.astimezone(UTC)


def quiz_rating(is_correct: bool) -> int:
    return GOOD if is_correct else AGAIN


def translation_rating(verdict: TranslationVerdict) -> int | None:
    """A missing sentence says nothing about memory, so it is not rated."""
    return {"correct": GOOD, "fixed": AGAIN, "missing": None}[verdict]


def due_at(state: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(state["due"])


class ReviewScheduler:
    def __init__(
        self, *, desired_retention: float = 0.9, maximum_interval_days: int = 180, enable_fuzzing: bool = True
    ) -> None:
        self._scheduler = Scheduler(
            desired_retention=desired_retention,
            learning_steps=(),
            relearning_steps=(),
            maximum_interval=maximum_interval_days,
            enable_fuzzing=enable_fuzzing,
        )

    def new_card(self, at: datetime) -> dict[str, Any]:
        """A card that is due right away."""
        return Card(due=utc(at)).to_dict()

    def review(self, state: dict[str, Any], rating: int, at: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply one rating; returns the new state and FSRS's own review log, both JSON-ready."""
        card, log = self._scheduler.review_card(Card.from_dict(state), Rating(rating), review_datetime=utc(at))
        return card.to_dict(), log.to_dict()

    def retrievability(self, state: dict[str, Any], at: datetime) -> float:
        """Probability (0..1) that the learner still recalls the item at `at`."""
        return float(self._scheduler.get_card_retrievability(Card.from_dict(state), utc(at)))


def make_scheduler(settings: Settings) -> ReviewScheduler:
    return ReviewScheduler(
        desired_retention=settings.fsrs_desired_retention, maximum_interval_days=settings.fsrs_maximum_interval_days
    )
