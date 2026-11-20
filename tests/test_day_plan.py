from __future__ import annotations

from datetime import UTC, date, datetime

from english_agent.day_plan import fallback_plan, frontier_known
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import load_curriculum
from english_agent.models.storage import SentStep
from english_agent.progression import next_step, topic_progress
from english_agent.topic_graph import default_graph

DAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)


def plan(cefr: Cefr, known: set[str]) -> tuple[str, str]:
    made = fallback_plan(
        learner_id=1,
        day=DAY,
        cefr=cefr,
        curriculum=load_curriculum(cefr),
        graph=default_graph(),
        progress={},
        skipped=set(),
        accuracy={},
        known=known,
        now=NOW,
    )
    assert made is not None and made.step is not None and made.source == "fallback"
    return made.day_type, made.step.topic_id


def test_a_new_learner_starts_at_the_first_topic() -> None:
    assert plan("A2", set()) == ("new_topic", "be_present")


def test_known_topics_are_skipped_and_a_topic_waits_for_its_prerequisites() -> None:
    a2 = sorted(load_curriculum("A2").topics, key=lambda topic: topic.order)
    assert plan("A2", {"be_present"}) == ("new_topic", "have_got")
    # Topic 10 needs "questions with do/does/did" (topic 39): with 1-9 known, topic 11 comes first.
    first_nine = {topic.id for topic in a2[:9]}
    assert a2[9].id == "past_simple_questions_negatives"
    assert plan("A2", first_nine) == ("new_topic", a2[10].id)
    assert plan("A2", first_nine | {"question_forms_do_support"}) == ("new_topic", a2[9].id)


def test_a_learner_placed_at_a_level_knows_the_levels_below() -> None:
    graph = default_graph()
    known = frontier_known(graph, "B1", {}, set())
    assert "be_present" in known and "present_tenses_review" not in known
    assert plan("B1", set()) == ("new_topic", "present_tenses_review")


def test_with_nothing_on_the_frontier_the_order_decides() -> None:
    curriculum = load_curriculum("A2")
    found = next_step(curriculum, {}, set(), {}, frontier=set())
    assert found is not None and found.topic.id == "be_present"
    done = {topic.id for topic in curriculum.topics}
    assert next_step(curriculum, {}, done, {}, frontier=set()) is None  # every topic skipped: nothing left


def test_a_topic_in_progress_is_finished_before_an_earlier_untouched_one() -> None:
    curriculum = load_curriculum("A2")
    later = SentStep(
        lesson_id=1,
        topic_id="past_continuous",
        lesson_date=DAY,
        step_index=1,
        step_count=2,
        step_slug="s1",
        variant="main",
        content_id=5,
    )
    found = next_step(curriculum, topic_progress([later]), set(), {}, frontier={"be_present"})
    assert found is not None and (found.topic.id, found.index) == ("past_continuous", 2)
