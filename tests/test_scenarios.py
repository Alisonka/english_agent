from __future__ import annotations

from english_agent.models.plan import DayType
from english_agent.scenarios import Scenario, fallback_decider, load_scenarios, report, run_scenarios


async def test_the_fallback_policy_passes_every_scenario_where_the_rules_are_clear() -> None:
    scenarios = load_scenarios().scenarios
    assert len({s.id for s in scenarios}) == len(scenarios)

    results = await run_scenarios(fallback_decider, scenarios)

    failed = [r.id for r in results if r.rules_clear and not r.ok]
    assert failed == [], report(results)
    assert any(not r.rules_clear for r in results)  # the set also measures judgement


async def test_the_report_counts_by_class() -> None:
    async def always_review(scenario: Scenario) -> DayType | None:
        return "review_day"

    results = await run_scenarios(always_review, load_scenarios().scenarios)
    summary = report(results).splitlines()[-1]
    assert summary.startswith("all: ") and "rules clear: 0/" in summary
