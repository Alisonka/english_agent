"""Mentor decision scenarios: a learner's state, and the day types that are acceptable for it.

Each scenario is a compact snapshot (level, topics done, the step in progress and how it went, days
away, known topics) with the classes of decision a good tutor may take. `run_scenarios` asks a
decider for each one and reports the share it got within the allowed classes. Scenarios marked
`rules_clear` are ones the deterministic policy must already pass; the others need judgement and
measure the agent (and shadow-mode disagreements become new ones).

    uv run python -m english_agent.scenarios            # the fallback policy on the set
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from english_agent.day_plan import fallback_plan
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import load_curriculum
from english_agent.models.plan import DayType
from english_agent.models.storage import ItemAccuracy, SentStep
from english_agent.paths import repo_root
from english_agent.progression import topic_progress
from english_agent.topic_graph import default_graph

DAY = date(2026, 11, 20)


class StepInProgress(BaseModel):
    topic_id: str
    steps_sent: int = Field(ge=1)
    step_count: int = Field(ge=1, le=4)
    last_checks: int = 0  # reviews of the last sent step
    last_correct: int = 0
    last_days: int = 0


class Scenario(BaseModel):
    id: str
    note: str
    cefr: Cefr = "A2"
    done_topics: list[str] = Field(default_factory=list)
    in_progress: StepInProgress | None = None
    known_topics: list[str] = Field(default_factory=list)  # credited or mastered beyond `done_topics`
    days_away: int = 0
    allowed: list[DayType] = Field(min_length=1)
    rules_clear: bool = False  # the deterministic policy is expected to pass it


class ScenarioSet(BaseModel):
    version: int
    scenarios: list[Scenario]


Decider = Callable[[Scenario], Awaitable[DayType | None]]


def scenarios_path() -> Path:
    return repo_root() / "evals" / "datasets" / "mentor-scenarios-v1.yaml"


def load_scenarios(path: Path | None = None) -> ScenarioSet:
    return ScenarioSet.model_validate(yaml.safe_load((path or scenarios_path()).read_text(encoding="utf-8")))


def sent_steps(scenario: Scenario) -> tuple[list[SentStep], dict[str, ItemAccuracy]]:
    """The scenario as the progression sees it: sent steps and the last step's accuracy."""
    rows: list[SentStep] = []
    accuracy: dict[str, ItemAccuracy] = {}
    lesson_id = 0

    def sent(topic: str, index: int, count: int) -> SentStep:
        nonlocal lesson_id
        lesson_id += 1
        return SentStep(
            lesson_id=lesson_id,
            topic_id=topic,
            lesson_date=DAY,
            step_index=index,
            step_count=count,
            step_slug=f"s{index}",
            variant="main",
            content_id=lesson_id,
        )

    for topic in scenario.done_topics:
        rows.append(sent(topic, 1, 1))
    current = scenario.in_progress
    if current is not None:
        for index in range(1, current.steps_sent + 1):
            rows.append(sent(current.topic_id, index, current.step_count))
        last = rows[-1]
        accuracy[last.item_key] = ItemAccuracy(
            item_id=last.lesson_id, checks=current.last_checks, correct=current.last_correct, days=current.last_days
        )
    return rows, accuracy


async def fallback_decider(scenario: Scenario) -> DayType | None:
    rows, accuracy = sent_steps(scenario)
    plan = fallback_plan(
        learner_id=1,
        day=DAY,
        cefr=scenario.cefr,
        curriculum=load_curriculum(scenario.cefr),
        graph=default_graph(),
        progress=topic_progress(rows),
        skipped=set(),
        accuracy=accuracy,
        known=scenario.known_topics,
        now=datetime.combine(DAY, datetime.min.time(), UTC),
    )
    return plan.day_type if plan is not None else None


class ScenarioResult(BaseModel):
    id: str
    decided: DayType | None
    allowed: list[DayType]
    rules_clear: bool

    @property
    def ok(self) -> bool:
        return self.decided in self.allowed


async def run_scenarios(decide: Decider, scenarios: list[Scenario]) -> list[ScenarioResult]:
    return [
        ScenarioResult(id=s.id, decided=await decide(s), allowed=s.allowed, rules_clear=s.rules_clear)
        for s in scenarios
    ]


def report(results: list[ScenarioResult]) -> str:
    def share(rows: list[ScenarioResult]) -> str:
        return f"{sum(r.ok for r in rows)}/{len(rows)}" if rows else "0/0"

    lines = [f"{'ok' if r.ok else 'NO':>2}  {r.id}: {r.decided} (allowed: {', '.join(r.allowed)})" for r in results]
    clear = [r for r in results if r.rules_clear]
    judgement = [r for r in results if not r.rules_clear]
    lines.append(f"all: {share(results)}, rules clear: {share(clear)}, judgement: {share(judgement)}")
    return "\n".join(lines)


def main() -> int:
    results = asyncio.run(run_scenarios(fallback_decider, load_scenarios().scenarios))
    print(report(results))
    return 0 if all(r.ok for r in results if r.rules_clear) else 1


if __name__ == "__main__":
    raise SystemExit(main())
