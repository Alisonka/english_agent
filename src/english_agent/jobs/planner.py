"""One scheduler tick: what is due right now, read from PostgreSQL in a handful of queries.

`plan_tick` only decides; `tasks.schedule_tick` enqueues. Keeping them apart lets the plan be
tested against in-memory stores without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from english_agent.grading import MAX_GRADING_ATTEMPTS
from english_agent.models.schedule import local_now
from english_agent.pipeline import Runtime
from english_agent.planning import lesson_due, practice_due

# A grading attempt that has not finished after this long belongs to a dead worker (one attempt
# is at most LLM_TIMEOUT_SECONDS x (LLM_MAX_RETRIES + 1) x 2 model calls).
GRADING_STALE_MINUTES = 15


@dataclass
class Plan:
    lessons: list[tuple[int, date]] = field(default_factory=list)  # (learner id, local day)
    practice: list[tuple[int, date]] = field(default_factory=list)
    gradings: list[int] = field(default_factory=list)  # answer ids to (re)grade
    abandoned_gradings: list[int] = field(default_factory=list)  # answer ids out of attempts


async def plan_tick(runtime: Runtime, now: datetime) -> Plan:
    stores, settings = runtime.stores, runtime.settings
    learners = await stores.learners.list_active()
    days = {learner.id: local_now(learner.timezone, now).date() for learner in learners}
    states = await stores.lessons.states([(learner.id, days[learner.id]) for learner in learners])
    claimed = await stores.practice.delivered_kinds([state.lesson_id for state in states.values()])

    plan = Plan()
    for learner in learners:
        day = days[learner.id]
        state = states.get((learner.id, day))
        if lesson_due(learner, state, now, settings):
            plan.lessons.append((learner.id, day))
        kinds = claimed.get(state.lesson_id, set()) if state is not None else set()
        if practice_due(learner, state, kinds, now, settings):
            plan.practice.append((learner.id, day))

    for answer_id, attempts in await stores.practice.stale_gradings(now - timedelta(minutes=GRADING_STALE_MINUTES)):
        (plan.gradings if attempts < MAX_GRADING_ATTEMPTS else plan.abandoned_gradings).append(answer_id)
    return plan
