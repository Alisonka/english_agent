import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from english_agent.config import Settings
from english_agent.llm import AssistantTurn, LLMRejected, LLMUnavailable, ToolCall
from english_agent.llm.anthropic_client import (
    AnthropicLLM,
    parse_response_blocks,
    to_anthropic_messages,
    to_anthropic_tool_choice,
    to_anthropic_tools,
)


def test_transcript_conversion_merges_tool_results_and_keeps_thinking_blocks() -> None:
    thinking = {"type": "thinking", "thinking": "...", "signature": "sig"}
    tool_use = {"type": "tool_use", "id": "t1", "name": "search_site", "input": {"site": "englex"}}
    turn = parse_response_blocks(
        [thinking, tool_use, {"type": "tool_use", "id": "t2", "name": "fetch_page", "input": {}}]
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "topic"},
        turn.to_message(),
        {"role": "tool", "tool_call_id": "t1", "content": "[]"},
        {"role": "tool", "tool_call_id": "t2", "content": "{}"},
        {"role": "user", "content": "go on"},
    ]
    system, converted = to_anthropic_messages(messages)

    assert system == "sys"
    assert [m["role"] for m in converted] == ["user", "assistant", "user", "user"]
    assert converted[1]["content"][0] == thinking  # replayed verbatim, signature intact
    results = converted[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["type"] == "tool_result" for r in results)


def test_parse_response_blocks_extracts_text_and_calls() -> None:
    turn = parse_response_blocks(
        [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "c1", "name": "finish", "input": {"found": False}}]
    )
    assert turn.content == "ok"
    assert turn.tool_calls == [ToolCall(id="c1", name="finish", arguments={"found": False})]


def test_scripted_turn_without_raw_blocks_serializes_to_tool_use_blocks() -> None:
    turn = AssistantTurn(content=None, tool_calls=[ToolCall(id="c1", name="finish", arguments={"found": True})])
    assert turn.to_message() == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "finish", "input": {"found": True}}],
    }


def test_tool_and_tool_choice_conversion() -> None:
    spec = {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}
    assert to_anthropic_tools([spec]) == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]
    assert to_anthropic_tool_choice("auto") == {"type": "auto"}
    assert to_anthropic_tool_choice({"type": "function", "function": {"name": "f"}}) == {"type": "tool", "name": "f"}


class _RecordingMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(usage=None, stop_reason="end_turn", content=[])


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="k", **overrides)


def _llm(effort: str | None) -> tuple[AnthropicLLM, _RecordingMessages]:
    messages = _RecordingMessages()
    client = cast(AsyncAnthropic, SimpleNamespace(messages=messages))
    return AnthropicLLM(_settings(), "claude-haiku-5-5", effort=effort, client=client), messages


async def test_effort_goes_into_output_config() -> None:
    llm, messages = _llm("high")
    await llm.chat([{"role": "user", "content": "hi"}], [])
    assert messages.kwargs["output_config"] == {"effort": "high"}


async def test_no_effort_keeps_the_model_default() -> None:
    llm, messages = _llm(None)
    await llm.chat([{"role": "user", "content": "hi"}], [])
    assert "output_config" not in messages.kwargs


async def test_every_request_marks_the_prompt_for_caching() -> None:
    llm, messages = _llm(None)
    await llm.chat([{"role": "user", "content": "hi"}], [])
    assert messages.kwargs["cache_control"] == {"type": "ephemeral"}


def _http_llm(handler: Any, **settings: Any) -> AnthropicLLM:
    """An AnthropicLLM over the real SDK, answered by `handler` instead of the network (no SDK retries)."""
    client = AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicLLM(_settings(**settings), "claude-haiku-5-5", client=client)


async def test_a_real_response_is_parsed_into_a_turn() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "tool_use", "id": "t1", "name": "finish", "input": {"found": False}}],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        return httpx2.Response(200, json=body)

    turn = await _http_llm(handler).chat([{"role": "user", "content": "hi"}], [])
    assert [(call.name, call.arguments) for call in turn.tool_calls] == [("finish", {"found": False})]


@pytest.mark.parametrize(("status", "expected"), [(429, LLMUnavailable), (529, LLMUnavailable), (500, LLMUnavailable)])
async def test_rate_limits_and_overload_are_retryable(status: int, expected: type[Exception]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    with pytest.raises(expected):
        await _http_llm(handler).chat([{"role": "user", "content": "hi"}], [])


async def test_a_bad_request_is_not_retryable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}})

    with pytest.raises(LLMRejected):
        await _http_llm(handler).chat([{"role": "user", "content": "hi"}], [])


async def test_the_gate_limits_requests_in_flight() -> None:
    in_flight, peak = 0, 0

    class SlowMessages:
        async def create(self, **kwargs: Any) -> SimpleNamespace:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return SimpleNamespace(usage=None, stop_reason="end_turn", content=[])

    client = cast(AsyncAnthropic, SimpleNamespace(messages=SlowMessages()))
    llm = AnthropicLLM(_settings(), "claude-haiku-5-5", client=client, gate=asyncio.Semaphore(2))
    await asyncio.gather(*(llm.chat([{"role": "user", "content": "hi"}], []) for _ in range(6)))
    assert peak == 2
