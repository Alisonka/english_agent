"""Synthetic learners with a hidden knowledge map, to compare deciders (the rules, the agent) over weeks.

Each learner knows some topics of the level already and has one planted gap in a prerequisite: while
the gap is open, every topic that depends on it is harder. A decider plans each day; the learner
answers the day's questions from the hidden map (P = logistic(skill - difficulty)); being taught a
topic raises its skill, a re-teach a little more, a remedial on the gap closes it. The metrics are
those of the report: was the gap found and closed (and on which day), mastery by the hidden map at
the end, the share of lessons spent on what the learner already knew, and the steps of new material.

The simulator answers by the same kind of model the agent leans on, so its numbers flatter the
agent: they compare versions, they do not promise anything about people.

    uv run python -m english_agent.simulator --learners 200 --days 30 --seed 1
"""

from __future__ import annotations

import argparse
import random
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from english_agent.day_plan import fallback_plan
from english_agent.knowledge import probability
from english_agent.models.curriculum import load_curriculum
from english_agent.models.plan import DayType
from english_agent.models.storage import ItemAccuracy, SentStep
from english_agent.progression import topic_progress
from english_agent.topic_graph import TopicGraph, default_graph

START = date(2026, 12, 1)
STEPS = 2  # steps per simulated topic
QUESTIONS = 5  # answers per day on the day's topic
GAP_PENALTY = 1.5
TAUGHT_GAIN = 1.2  # per step taught
RETEACH_GAIN = 0.6
KNOWN_SKILL = 2.0


@dataclass
class SimLearner:
    gap_topic: str
    skill: dict[str, float]  # hidden, per topic
    known: set[str]  # topics known before the course (hidden from the decider)
    gap_open: bool = True
    gap_closed_on: int | None = None


@dataclass
class SimState:
    """What a decider sees: the course as the progression knows it, never the hidden map."""

    day: int
    sent: list[SentStep] = field(default_factory=list)
    accuracy: dict[str, ItemAccuracy] = field(default_factory=dict)
    known_topics: set[str] = field(default_factory=set)  # credited ones (none in this simulator)


@dataclass(frozen=True)
class DayChoice:
    day_type: DayType
    topic_id: str | None
    step_index: int = 1
    variant: str = "main"


Decider = Callable[[SimState], DayChoice | None]


def fallback_decider(state: SimState) -> DayChoice | None:
    plan = fallback_plan(
        learner_id=1,
        day=START + timedelta(days=state.day),
        cefr="A2",
        curriculum=load_curriculum("A2"),
        graph=default_graph(),
        progress=topic_progress(state.sent),
        skipped=set(),
        accuracy=state.accuracy,
        known=state.known_topics,
        now=datetime.combine(START, datetime.min.time(), UTC),
    )
    if plan is None or plan.step is None:
        return None
    return DayChoice(plan.day_type, plan.step.topic_id, plan.step.step_index, plan.step.variant)


def make_learner(rng: random.Random, graph: TopicGraph) -> SimLearner:
    topics = [node.id for node in graph.nodes.values() if node.cefr == "A2"]
    prerequisites = sorted({p for t in topics for p in graph.prerequisites(t) if graph.nodes[p].cefr == "A2"})
    gap = rng.choice(prerequisites)
    known = {t for t in topics if t != gap and rng.random() < 0.2}
    skill = {t: KNOWN_SKILL if t in known else -1.0 for t in topics}
    return SimLearner(gap_topic=gap, skill=skill, known=known)


def chance(learner: SimLearner, graph: TopicGraph, topic: str, difficulty: float = -1.0) -> float:
    skill = learner.skill.get(topic, -1.0)
    blocked = learner.gap_open and (topic == learner.gap_topic or learner.gap_topic in _ancestors(graph, topic))
    return probability(skill - (GAP_PENALTY if blocked else 0.0), [], difficulty)


def _ancestors(graph: TopicGraph, topic: str) -> set[str]:
    seen: set[str] = set()
    todo = list(graph.prerequisites(topic)) if topic in graph.nodes else []
    while todo:
        current = todo.pop()
        if current not in seen:
            seen.add(current)
            todo.extend(graph.prerequisites(current))
    return seen


@dataclass(frozen=True)
class SimOutcome:
    gap_closed_on: int | None
    mastery: float  # mean chance over the level's topics by the hidden map, at the end
    wasted_share: float  # lessons on topics known before the course
    new_steps: int


def simulate(decide: Decider, learner: SimLearner, graph: TopicGraph, days: int, rng: random.Random) -> SimOutcome:
    state = SimState(day=0)
    lessons = wasted = new_steps = 0
    for day in range(days):
        state.day = day
        choice = decide(state)
        if choice is None or choice.topic_id is None:
            continue
        topic = choice.topic_id
        lessons += 1
        wasted += topic in learner.known
        if choice.day_type == "remedial" and topic == learner.gap_topic:
            learner.gap_open, learner.gap_closed_on = False, day
        elif choice.variant == "alt":
            learner.skill[topic] = learner.skill.get(topic, -1.0) + RETEACH_GAIN
            if topic == learner.gap_topic:  # explaining the gap topic again closes it
                learner.gap_open, learner.gap_closed_on = False, day
        else:
            learner.skill[topic] = learner.skill.get(topic, -1.0) + TAUGHT_GAIN
            new_steps += 1
        index = len([s for s in state.sent if s.topic_id == topic]) + 1
        step = SentStep(
            lesson_id=day + 1,
            topic_id=topic,
            lesson_date=START + timedelta(days=day),
            step_index=choice.step_index if choice.variant == "alt" else min(index, STEPS),
            step_count=STEPS,
            step_slug=f"s{choice.step_index if choice.variant == 'alt' else min(index, STEPS)}",
            variant="alt" if choice.variant == "alt" else "main",
            content_id=1,
        )
        state.sent.append(step)
        right = sum(rng.random() < chance(learner, graph, topic) for _ in range(QUESTIONS))
        key = f"{topic}:s{step.step_index}"
        old = state.accuracy.get(key)
        state.accuracy[key] = ItemAccuracy(
            item_id=day + 1,
            checks=(old.checks if old else 0) + QUESTIONS,
            correct=(old.correct if old else 0) + right,
            days=(old.days if old else 0) + 1,
        )
    topics = list(learner.skill)
    mastery = statistics.fmean(chance(learner, graph, topic) for topic in topics)
    return SimOutcome(learner.gap_closed_on, mastery, wasted / lessons if lessons else 0.0, new_steps)


@dataclass(frozen=True)
class SimReport:
    learners: int
    gaps_closed: int
    median_close_day: float | None
    mastery: float
    wasted_share: float
    new_steps: float

    def render(self, name: str) -> str:
        median = f"{self.median_close_day:.0f}" if self.median_close_day is not None else "-"
        return (
            f"{name}: gap closed {self.gaps_closed}/{self.learners} ({self.gaps_closed / self.learners:.0%}, "
            f"median day {median}); mastery {self.mastery:.2f}; lessons on known {self.wasted_share:.0%}; "
            f"new steps {self.new_steps:.1f}"
        )


def run(decide: Decider, *, learners: int, days: int, seed: int) -> SimReport:
    graph = default_graph()
    rng = random.Random(seed)
    outcomes = [simulate(decide, make_learner(rng, graph), graph, days, rng) for _ in range(learners)]
    closed = [o.gap_closed_on for o in outcomes if o.gap_closed_on is not None]
    return SimReport(
        learners=learners,
        gaps_closed=len(closed),
        median_close_day=statistics.median(closed) if closed else None,
        mastery=statistics.fmean(o.mastery for o in outcomes),
        wasted_share=statistics.fmean(o.wasted_share for o in outcomes),
        new_steps=statistics.fmean(o.new_steps for o in outcomes),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic learners through the fallback policy.")
    parser.add_argument("--learners", type=int, default=200)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    print(run(fallback_decider, learners=args.learners, days=args.days, seed=args.seed).render("rules"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
