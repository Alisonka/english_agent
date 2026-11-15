from __future__ import annotations

from datetime import date

from english_agent.models.curriculum import load_curriculum
from english_agent.models.storage import ItemAccuracy, SentStep
from english_agent.progression import is_done, next_step, reteach_candidate, topic_progress

CURRICULUM = load_curriculum("A2")
ORDERED = [topic.id for topic in sorted(CURRICULUM.topics, key=lambda topic: topic.order)]
FIRST, SECOND = ORDERED[0], ORDERED[1]
DAY = date(2026, 11, 1)


def _step(topic: str, index: int, count: int, slug: str, variant: str = "main", content_id: int = 1) -> SentStep:
    return SentStep(
        lesson_id=index,
        topic_id=topic,
        lesson_date=DAY,
        step_index=index,
        step_count=count,
        step_slug=slug,
        variant=variant,
        content_id=content_id,
    )


def test_fresh_learner_starts_the_first_topic() -> None:
    nxt = next_step(CURRICULUM, {}, set(), {})
    assert nxt is not None and nxt.topic.id == FIRST and nxt.index == 1 and nxt.content_id is None


def test_legacy_single_step_topic_counts_as_done() -> None:
    progress = topic_progress([_step(FIRST, 1, 1, "core")])
    assert is_done(progress[FIRST])
    nxt = next_step(CURRICULUM, progress, set(), {})
    assert nxt is not None and nxt.topic.id == SECOND and nxt.leaving == FIRST


def test_mid_topic_advances_to_the_next_index_with_pinned_content() -> None:
    progress = topic_progress([_step(FIRST, 1, 2, "s1", content_id=5)])
    nxt = next_step(CURRICULUM, progress, set(), {})
    assert nxt is not None and nxt.topic.id == FIRST and nxt.index == 2 and nxt.variant == "main"
    assert nxt.content_id == 5 and nxt.leaving is None


def test_low_accuracy_step_is_reteught_before_advancing() -> None:
    progress = topic_progress([_step(FIRST, 1, 2, "s1", content_id=5)])
    accuracy = {f"{FIRST}:s1": ItemAccuracy(item_id=1, checks=3, correct=1, days=2)}
    nxt = next_step(CURRICULUM, progress, set(), accuracy)
    assert nxt is not None and nxt.variant == "alt" and nxt.index == 1


def test_reteach_needs_two_checks_on_two_days() -> None:
    progress = topic_progress([_step(FIRST, 1, 2, "s1")])
    one_day = {f"{FIRST}:s1": ItemAccuracy(item_id=1, checks=3, correct=0, days=1)}
    assert reteach_candidate(progress[FIRST], one_day) is None  # all on one day: not yet
    enough = {f"{FIRST}:s1": ItemAccuracy(item_id=1, checks=2, correct=0, days=2)}
    assert reteach_candidate(progress[FIRST], enough) == "s1"


def test_a_step_is_reteught_at_most_once() -> None:
    progress = topic_progress(
        [_step(FIRST, 1, 2, "s1"), _step(FIRST, 2, 2, "s2"), _step(FIRST, 1, 2, "s1", variant="alt")]
    )
    accuracy = {f"{FIRST}:s1": ItemAccuracy(item_id=1, checks=3, correct=0, days=2)}
    assert reteach_candidate(progress[FIRST], accuracy) is None  # already has an alt row


def test_skipped_topic_is_passed_over() -> None:
    nxt = next_step(CURRICULUM, {}, {FIRST}, {})
    assert nxt is not None and nxt.topic.id == SECOND


def test_none_when_every_topic_is_done_or_skipped() -> None:
    progress = topic_progress([_step(topic, 1, 1, "core") for topic in ORDERED])
    assert next_step(CURRICULUM, progress, set(), {}) is None
