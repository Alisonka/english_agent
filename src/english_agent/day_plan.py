"""The deterministic day plan: the course logic from before the Mentor, now its fallback.

Next topic from the frontier, a re-teach when a step's accuracy is below the threshold, the topic
in progress otherwise. A learner placed at a level knows the levels below it, and a topic whose
steps were all sent counts as known, so the frontier never holds back a learner the old order
would have moved on.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum
from english_agent.models.plan import DayPlan, DayType, FallbackBranch, StepChoice
from english_agent.models.storage import ItemAccuracy
from english_agent.progression import NextStep, TopicProgress, is_done, next_step
from english_agent.topic_graph import TopicGraph
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

EXPLANATIONS: dict[DayType, str] = {
    "new_topic": "Сегодня новая тема: {title}.",
    "continue_topic": "Продолжаем тему «{title}».",
    "reteach": "Ещё раз про «{title}», другими словами и на других примерах.",
}


def frontier_known(graph: TopicGraph, cefr: Cefr, progress: dict[str, TopicProgress], known: Iterable[str]) -> set[str]:
    """What counts as known for the frontier: statuses, finished topics, and every lower level."""
    below = set(CEFR_LEVELS[: CEFR_LEVELS.index(cefr)])
    lower = {node.id for node in graph.nodes.values() if node.cefr in below}
    done = {topic for topic, here in progress.items() if is_done(here)}
    return set(known) | done | lower


def plan_from_step(learner_id: int, day: date, nxt: NextStep, now: datetime) -> DayPlan:
    day_type: DayType = "reteach" if nxt.variant == "alt" else "continue_topic" if nxt.content_id else "new_topic"
    return DayPlan(
        learner_id=learner_id,
        plan_date=day,
        day_type=day_type,
        step=StepChoice(
            topic_id=nxt.topic.id,
            step_index=nxt.index,
            variant=nxt.variant,
            content_id=nxt.content_id,
            leaving_topic_id=nxt.leaving,
        ),
        explanation_ru=EXPLANATIONS[day_type].format(title=nxt.topic.title_ru),
        # A new topic may not be written by the morning: then the day is reviews only.
        fallback=FallbackBranch(day_type="review_day") if day_type == "new_topic" else None,
        source="fallback",
        created_at=now,
        provenance=build_provenance(component="fallback_policy", schema_version=SCHEMA_VERSIONS["day_plan"]),
    )


def fallback_plan(
    *,
    learner_id: int,
    day: date,
    cefr: Cefr,
    curriculum: Curriculum,
    graph: TopicGraph,
    progress: dict[str, TopicProgress],
    skipped: set[str],
    accuracy: dict[str, ItemAccuracy],
    known: Iterable[str],
    now: datetime,
) -> DayPlan | None:
    """The deterministic plan for `day`, or None once the level's course is finished."""
    frontier = set(graph.frontier(frontier_known(graph, cefr, progress, known), cefr=cefr))
    nxt = next_step(curriculum, progress, skipped, accuracy, frontier=frontier)
    return None if nxt is None else plan_from_step(learner_id, day, nxt, now)
