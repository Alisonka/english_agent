"""The tool loop every agent runs, and the one budget every model call is charged to.

    messages -> model turn -> tool calls (validated and executed by a handler) -> results -> again

The handler owns the agent's rules; the loop owns what is the same for all of them: the step cap,
the reminder when the model answers in prose, turning a `ToolError` into a tool result the model
can act on, one span per tool call, and (optionally) running a turn's independent calls at once.
A `Budget` caps model calls, cost, subagent calls and wall time across a whole session, including
the calls made inside its subagents.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode

from english_agent.llm.base import AssistantTurn, ChatLLM, LLMError, ToolCall
from english_agent.tracing import get_tracer


class ToolError(Exception):
    """A rule violation or a failed tool. Reported back to the model, never raised out of the loop."""


class BudgetExceeded(LLMError):
    """The session reached its limit of model calls, cost, subagent calls or time."""


@dataclass(frozen=True)
class Finish[T]:
    """What a handler returns for the terminal call: the loop stops and hands back `value`."""

    value: T


class Budget:
    """Caps shared by every model call of a session (and by the subagents it calls)."""

    def __init__(
        self,
        max_calls: int,
        max_cost_usd: float,
        *,
        max_subagent_calls: int | None = None,
        timeout_s: float | None = None,
        subject: str = "the run",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls, self.max_cost_usd = max_calls, max_cost_usd
        self.max_subagent_calls = max_subagent_calls
        self.calls, self.cost_usd, self.subagent_calls = 0, 0.0, 0
        self._subject, self._clock = subject, clock
        self._deadline = clock() + timeout_s if timeout_s is not None else None
        self._timeout_s = timeout_s

    def check(self) -> None:
        """Raise before a model call that the budget no longer allows."""
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"{self._subject} used its {self.max_calls} model calls")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"{self._subject} spent its ${self.max_cost_usd:.2f}")
        if self._deadline is not None and self._clock() >= self._deadline:
            raise BudgetExceeded(f"{self._subject} ran out of its {self._timeout_s:g} s")

    def charge(self, turn: AssistantTurn) -> float:
        """Count one finished model call; returns its cost (0 when the provider reported none)."""
        cost = turn.usage.cost_usd if turn.usage is not None and turn.usage.cost_usd is not None else 0.0
        self.calls += 1
        self.cost_usd += cost
        return cost

    def claim_subagent(self) -> None:
        """Count one subagent call, or raise when the session has used them all."""
        if self.max_subagent_calls is not None and self.subagent_calls >= self.max_subagent_calls:
            raise BudgetExceeded(f"{self._subject} used its {self.max_subagent_calls} subagent calls")
        self.check()
        self.subagent_calls += 1


class MeteredLLM:
    """A `ChatLLM` that checks the budget before every call and charges it after."""

    def __init__(self, inner: ChatLLM, budget: Budget) -> None:
        self._inner, self._budget = inner, budget

    @property
    def model(self) -> str | None:
        return getattr(self._inner, "model", None)

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn:
        self._budget.check()
        turn = await self._inner.chat(messages, tools, tool_choice=tool_choice)
        self._budget.charge(turn)
        return turn


# A handler validates and executes one call: a payload for the model, `Finish` to stop, `ToolError` to refuse.
ToolHandler = Callable[[ToolCall, int, Span], Awaitable[Any]]


def tool_message(call: ToolCall, payload: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": json.dumps(payload, ensure_ascii=False)}


async def _run_call(handle: ToolHandler, call: ToolCall, step: int, step_attribute: str) -> Any:
    with get_tracer().start_as_current_span(f"tool.{call.name}") as span:
        span.set_attribute(step_attribute, step)
        try:
            return await handle(call, step, span)
        except ToolError as error:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            return {"error": str(error)}


async def run_tool_loop[T](
    llm: ChatLLM,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    handle: ToolHandler,
    *,
    max_steps: int,
    nudge: str,
    step_attribute: str = "agent.step",
    parallel: bool = False,
) -> tuple[T | None, int]:
    """Run until a handler returns `Finish` (its value and the step) or the steps run out (None, max_steps).

    `messages` is extended in place, so the caller keeps the transcript. With `parallel`, a turn's
    calls run concurrently (for independent reads); their results still go back in the turn's order,
    and a `Finish` among them wins once the others are done.
    """
    for step in range(1, max_steps + 1):
        turn = await llm.chat(messages, tools)
        messages.append(turn.to_message())
        if not turn.tool_calls:
            messages.append({"role": "user", "content": nudge})
            continue
        if parallel:
            outcomes = await asyncio.gather(
                *(_run_call(handle, call, step, step_attribute) for call in turn.tool_calls)
            )
            finished = next((outcome for outcome in outcomes if isinstance(outcome, Finish)), None)
            if finished is not None:
                return finished.value, step
            messages.extend(
                tool_message(call, outcome) for call, outcome in zip(turn.tool_calls, outcomes, strict=True)
            )
            continue
        for call in turn.tool_calls:
            outcome = await _run_call(handle, call, step, step_attribute)
            if isinstance(outcome, Finish):
                return outcome.value, step
            messages.append(tool_message(call, outcome))
    return None, max_steps


async def run_subagent[T](budget: Budget, name: str, run: Callable[[], Awaitable[T]]) -> T:
    """Call a subagent as a tool: counted against the budget, in its own span, a failure becomes a `ToolError`.

    The orchestrator then sees "<name> failed: ..." as a tool result and decides without it.
    """
    with get_tracer().start_as_current_span(f"subagent.{name}") as span:
        try:
            budget.claim_subagent()
            return await run()
        except Exception as error:  # isolation: a subagent's failure must not end the session
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            raise ToolError(f"{name} failed: {error}") from error
