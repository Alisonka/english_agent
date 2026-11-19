from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from opentelemetry.trace import Span

from english_agent.llm import AssistantTurn, ToolCall, Usage
from english_agent.llm.agent_loop import (
    Budget,
    BudgetExceeded,
    Finish,
    MeteredLLM,
    ToolError,
    run_subagent,
    run_tool_loop,
)
from fakes import ScriptedLLM

NUDGE = "Use a tool."


def calls(*names: str, cost: float | None = None) -> AssistantTurn:
    return AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id=f"c{i}", name=name, arguments={}) for i, name in enumerate(names)],
        usage=Usage(cost_usd=cost) if cost is not None else None,
    )


def tool_results(messages: list[dict[str, Any]]) -> list[Any]:
    return [json.loads(message["content"]) for message in messages if message["role"] == "tool"]


async def echo(call: ToolCall, step: int, span: Span) -> Any:
    if call.name == "done":
        return Finish(f"done at {step}")
    if call.name == "bad":
        raise ToolError("not allowed")
    return {"tool": call.name}


async def test_finish_returns_the_value_and_the_step() -> None:
    llm = ScriptedLLM([calls("read"), calls("done")])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]

    result, step = await run_tool_loop(llm, messages, [], echo, max_steps=5, nudge=NUDGE)

    assert (result, step) == ("done at 2", 2)
    assert tool_results(messages) == [{"tool": "read"}]


async def test_a_tool_error_goes_back_to_the_model_and_the_loop_goes_on() -> None:
    llm = ScriptedLLM([calls("bad", "read"), calls("done")])
    messages: list[dict[str, Any]] = []

    result, _ = await run_tool_loop(llm, messages, [], echo, max_steps=5, nudge=NUDGE)

    assert result == "done at 2"
    assert tool_results(messages) == [{"error": "not allowed"}, {"tool": "read"}]


async def test_prose_gets_the_nudge_and_the_step_cap_ends_the_loop() -> None:
    llm = ScriptedLLM([AssistantTurn(content="thinking aloud"), calls("read"), calls("read")])
    messages: list[dict[str, Any]] = []

    result, step = await run_tool_loop(llm, messages, [], echo, max_steps=3, nudge=NUDGE)

    assert (result, step) == (None, 3)
    assert {"role": "user", "content": NUDGE} in messages
    assert len(llm.calls) == 3


async def test_parallel_calls_run_at_once_and_keep_their_order() -> None:
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def meet(call: ToolCall, step: int, span: Span) -> Any:
        if call.name == "done":
            return Finish("ok")
        started[call.name].set()
        other = "b" if call.name == "a" else "a"
        await asyncio.wait_for(started[other].wait(), timeout=1)  # deadlocks if run one after the other
        return call.name

    llm = ScriptedLLM([calls("b", "a"), calls("done")])
    messages: list[dict[str, Any]] = []

    result, _ = await run_tool_loop(llm, messages, [], meet, max_steps=3, nudge=NUDGE, parallel=True)

    assert result == "ok"
    assert tool_results(messages) == ["b", "a"]


async def test_metered_llm_stops_at_the_call_limit() -> None:
    budget = Budget(max_calls=2, max_cost_usd=10.0, subject="the session")
    llm = MeteredLLM(ScriptedLLM([calls("read", cost=0.25), calls("read", cost=0.5), calls("done")]), budget)

    with pytest.raises(BudgetExceeded, match="the session used its 2 model calls"):
        await run_tool_loop(llm, [], [], echo, max_steps=5, nudge=NUDGE)
    assert (budget.calls, budget.cost_usd) == (2, pytest.approx(0.75))


async def test_budget_stops_at_the_cost_limit_and_at_the_deadline() -> None:
    budget = Budget(max_calls=10, max_cost_usd=0.5)
    budget.charge(calls("read", cost=0.6))
    with pytest.raises(BudgetExceeded, match=r"\$0.50"):
        budget.check()

    now = [100.0]
    timed = Budget(max_calls=10, max_cost_usd=1.0, timeout_s=30, clock=lambda: now[0])
    timed.check()
    now[0] = 130.0
    with pytest.raises(BudgetExceeded, match="30 s"):
        timed.check()


async def test_a_subagent_failure_or_the_subagent_cap_is_a_tool_error() -> None:
    budget = Budget(max_calls=10, max_cost_usd=1.0, max_subagent_calls=2)

    async def ok() -> str:
        return "hypotheses"

    async def broken() -> str:
        raise RuntimeError("model is down")

    assert await run_subagent(budget, "diagnostician", ok) == "hypotheses"
    with pytest.raises(ToolError, match="diagnostician failed: model is down"):
        await run_subagent(budget, "diagnostician", broken)
    with pytest.raises(ToolError, match="2 subagent calls"):
        await run_subagent(budget, "diagnostician", ok)
    assert budget.subagent_calls == 2
