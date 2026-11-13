"""Anthropic Messages API behind the `ChatLLM` protocol.

Transcript format used by the agents (and translated here):
    {"role": "system", "content": str}
    {"role": "user", "content": str}
    {"role": "assistant", "content": [blocks]}          # from AssistantTurn.to_message()
    {"role": "tool", "tool_call_id": str, "content": str}  # one per tool call

Tool specs arrive in OpenAI "function" shape (what `tool_specs()` produces) and
are converted to Anthropic `input_schema` tools. Haiku 5.5 notes: no
`temperature`; thinking is adaptive by default and its blocks must be replayed,
which `AssistantTurn.raw_blocks` guarantees.

Production behaviour: one `AsyncAnthropic` client is shared by every call of a
process (its connection pool), each request has `LLM_TIMEOUT_SECONDS` and the
SDK retries 408/409/429/5xx/overloaded and connection errors `LLM_MAX_RETRIES`
times with backoff. What still fails is raised as `LLMUnavailable` (retry the
job later) or `LLMRejected` (do not). A per-event-loop semaphore keeps at most
`LLM_MAX_CONCURRENCY` requests of the process in flight, so a burst of lessons
or grades queues here instead of tripping the account's rate limit. Top-level
prompt caching marks the last block of every request, so the research loop,
which resends the whole transcript (fetched pages included) on every step,
reads the earlier steps from the cache.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from weakref import WeakKeyDictionary

import anthropic
from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.llm.base import AssistantTurn, LLMRejected, LLMUnavailable, ToolCall
from english_agent.llm.pricing import estimate_cost_usd
from english_agent.metrics import LLM_COST, LLM_ERRORS, LLM_TOKENS
from english_agent.tracing import get_tracer, record_model_output

_CONNECT_TIMEOUT_SECONDS = 10.0
_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


def make_client(settings: Settings) -> AsyncAnthropic:
    """The process's Claude API client; close it (`await client.close()`, or `async with`) on shutdown."""
    if not settings.anthropic_api_key.get_secret_value():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=anthropic.Timeout(settings.llm_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        max_retries=settings.llm_max_retries,
    )


def llm_gate(settings: Settings) -> asyncio.Semaphore:
    """The running event loop's limit on Claude API requests in flight (one per loop, created on first use)."""
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = _GATES[loop] = asyncio.Semaphore(settings.llm_max_concurrency)
    return gate


def classify_error(error: anthropic.APIError) -> LLMUnavailable | LLMRejected:
    """Retryable or not, after the SDK has used up its own retries."""
    if isinstance(error, anthropic.APIConnectionError):  # includes APITimeoutError
        return LLMUnavailable(f"{type(error).__name__}: {error}")
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        if status in (408, 409, 429) or status >= 500:  # 529 is OverloadedError
            return LLMUnavailable(f"HTTP {status} {type(error).__name__}: {error.message}")
        return LLMRejected(f"HTTP {status} {type(error).__name__}: {error.message}")
    return LLMUnavailable(f"{type(error).__name__}: {error}")


def to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = ""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system = message["content"]
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # all results for one assistant turn go in one user message
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": role, "content": message["content"]})
    return system, out


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"]["parameters"],
        }
        for tool in tools
    ]


def to_anthropic_tool_choice(tool_choice: Any) -> dict[str, Any]:
    if isinstance(tool_choice, dict):
        return {"type": "tool", "name": tool_choice["function"]["name"]}
    return {"type": str(tool_choice)}


def parse_response_blocks(blocks: list[dict[str, Any]]) -> AssistantTurn:
    text = "\n".join(block["text"] for block in blocks if block.get("type") == "text").strip() or None
    calls = [
        ToolCall(id=block["id"], name=block["name"], arguments=dict(block.get("input") or {}))
        for block in blocks
        if block.get("type") == "tool_use"
    ]
    return AssistantTurn(content=text, tool_calls=calls, raw_blocks=blocks)


class AnthropicLLM:
    def __init__(
        self,
        settings: Settings,
        model: str | None = None,
        *,
        effort: str | None = None,
        client: AsyncAnthropic | None = None,
        gate: asyncio.Semaphore | None = None,
    ) -> None:
        """`client`: the process's shared client (else a new one); `gate`: else `llm_gate(settings)` per call."""
        self.model = model or settings.research_model
        self.max_tokens = settings.llm_max_tokens
        self.effort = effort  # `output_config.effort`; None keeps the model's default
        self._settings = settings
        self._client = client or make_client(settings)
        self._gate = gate

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn:
        system, converted = to_anthropic_messages(messages)
        # Attributes follow OTel GenAI semantic conventions. Prompt/response *text*
        # is intentionally omitted (size and privacy); only shapes and counts.
        with get_tracer().start_as_current_span("llm.chat") as span:
            span.set_attribute("gen_ai.system", "anthropic")
            span.set_attribute("gen_ai.request.model", self.model)
            span.set_attribute("gen_ai.request.max_tokens", self.max_tokens)
            extra: dict[str, Any] = {}
            if self.effort is not None:
                span.set_attribute("gen_ai.request.effort", self.effort)
                extra["output_config"] = {"effort": self.effort}
            gate = self._gate or llm_gate(self._settings)
            try:
                async with gate:
                    response = await self._client.messages.create(  # type: ignore[call-overload]
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system,
                        messages=converted,
                        tools=to_anthropic_tools(tools),
                        tool_choice=to_anthropic_tool_choice(tool_choice),
                        cache_control={"type": "ephemeral"},
                        **extra,
                    )
            except anthropic.APIError as error:
                classified = classify_error(error)
                LLM_ERRORS.labels(type(classified).__name__).inc()
                span.record_exception(error)
                span.set_attribute("error.type", type(classified).__name__)
                raise classified from error
            if response.usage is not None:
                usage = response.usage
                cache_write = usage.cache_creation_input_tokens or 0
                cache_read = usage.cache_read_input_tokens or 0
                LLM_TOKENS.labels(self.model, "input").inc(usage.input_tokens)
                LLM_TOKENS.labels(self.model, "output").inc(usage.output_tokens)
                LLM_TOKENS.labels(self.model, "cache_read").inc(cache_read)
                LLM_TOKENS.labels(self.model, "cache_write").inc(cache_write)
                span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
                if cache_write or cache_read:
                    span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read)
                    span.set_attribute("gen_ai.usage.cache_creation_input_tokens", cache_write)
                cost = estimate_cost_usd(
                    self.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_write_tokens=cache_write,
                    cache_read_tokens=cache_read,
                )
                if cost is not None:
                    LLM_COST.labels(self.model).inc(cost)
                    span.set_attribute("gen_ai.usage.cost_usd", round(cost, 6))
            if response.stop_reason is not None:
                span.set_attribute("gen_ai.response.finish_reason", response.stop_reason)
            blocks = [json.loads(block.model_dump_json(exclude_none=True)) for block in response.content]
            turn = parse_response_blocks(blocks)
            span.set_attribute("gen_ai.response.tool_calls", len(turn.tool_calls))
            record_model_output(
                span,
                turn.content,
                [{"name": call.name, "arguments": call.arguments} for call in turn.tool_calls],
            )
            return turn
