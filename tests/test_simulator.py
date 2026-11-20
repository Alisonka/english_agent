from __future__ import annotations

import random

from english_agent.simulator import (
    DayChoice,
    Decider,
    SimLearner,
    SimState,
    fallback_decider,
    make_learner,
    run,
    simulate,
)
from english_agent.topic_graph import default_graph


def test_a_run_is_reproducible_with_the_same_seed() -> None:
    first = run(fallback_decider, learners=4, days=8, seed=7)
    again = run(fallback_decider, learners=4, days=8, seed=7)
    assert first == again
    assert first.new_steps > 0 and 0.0 <= first.wasted_share <= 1.0


def test_a_decider_that_repairs_the_gap_closes_it_and_learns_more() -> None:
    graph = default_graph()

    def oracle_for(learner: SimLearner) -> Decider:
        def decide(state: SimState) -> DayChoice | None:
            if state.day == 0:
                return DayChoice("remedial", learner.gap_topic)
            return fallback_decider(state)

        return decide

    rng = random.Random(3)
    learner = make_learner(rng, graph)
    twin = SimLearner(learner.gap_topic, dict(learner.skill), set(learner.known))
    repaired = simulate(oracle_for(learner), learner, graph, 10, random.Random(5))
    left = simulate(fallback_decider, twin, graph, 10, random.Random(5))

    assert repaired.gap_closed_on == 0 and left.gap_closed_on is None
    assert repaired.mastery > left.mastery
