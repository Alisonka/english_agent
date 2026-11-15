from __future__ import annotations

from datetime import UTC, datetime, timedelta

from english_agent.models.storage import ItemAccuracy
from english_agent.planner import MAX_REVIEW_SLOTS, Candidate, plan_quiz, plan_translation

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def older(item_id: int, *, due_in_days: float, recall: float = 0.5, checks: int = 0, correct: int = 0) -> Candidate:
    accuracy = ItemAccuracy(item_id=item_id, checks=checks, correct=correct, days=1) if checks else None
    return Candidate(
        item_id=item_id,
        item_key=f"old:{item_id}",
        same_topic=False,
        due_at=NOW + timedelta(days=due_in_days),
        retrievability=recall,
        accuracy=accuracy,
    )


def earlier(item_id: int, recall: float = 0.0) -> Candidate:
    return Candidate(item_id=item_id, item_key=f"today:{item_id}", same_topic=True, retrievability=recall)


def test_a_first_topic_has_nothing_to_review() -> None:
    plan = plan_quiz([], NOW)
    assert plan.previous is None and plan.reviews == [] and plan.review_slots == 0


def test_the_previous_step_is_the_least_recalled_earlier_one() -> None:
    plan = plan_quiz([earlier(1, 0.9), earlier(2, 0.3), earlier(3, 0.6)], NOW)
    assert plan.previous is not None and plan.previous.candidate.item_id == 2
    assert plan.previous.reason == "previous" and plan.previous.source == "previous"


def test_due_items_come_first_most_overdue_first_and_set_the_slots() -> None:
    candidates = [older(1, due_in_days=3), older(2, due_in_days=-1), older(3, due_in_days=-5), older(4, due_in_days=2)]
    plan = plan_quiz(candidates, NOW)
    assert [pick.candidate.item_id for pick in plan.reviews[:2]] == [3, 2]
    assert [pick.reason for pick in plan.reviews[:2]] == ["due", "due"]
    assert plan.review_slots == 2
    assert all(pick.source == "review" for pick in plan.reviews)


def test_at_least_one_older_item_even_when_nothing_is_due_and_the_least_recalled_wins() -> None:
    plan = plan_quiz([older(1, due_in_days=4, recall=0.8), older(2, due_in_days=9, recall=0.4)], NOW)
    assert plan.review_slots == 1
    assert plan.reviews[0].candidate.item_id == 2 and plan.reviews[0].reason == "least_recalled"


def test_review_slots_are_capped_for_a_learner_far_behind() -> None:
    plan = plan_quiz([older(i, due_in_days=-i) for i in range(1, 10)], NOW)
    assert plan.review_slots == MAX_REVIEW_SLOTS
    assert len(plan.reviews) == 9  # the caller skips items without a bank


def test_an_item_the_learner_keeps_getting_wrong_is_marked_weak() -> None:
    plan = plan_quiz([older(1, due_in_days=4, recall=0.3, checks=5, correct=2)], NOW)
    assert plan.reviews[0].reason == "weak"


def test_translation_prefers_what_went_wrong_today_then_due_then_the_previous_step() -> None:
    candidates = [older(1, due_in_days=-2), older(2, due_in_days=5), earlier(3), older(4, due_in_days=1)]
    ranked = plan_translation(candidates, NOW, wrong_today={4})
    assert [(pick.candidate.item_id, pick.reason) for pick in ranked] == [
        (4, "wrong_today"),
        (1, "due"),
        (3, "previous"),
        (2, "least_recalled"),
    ]
