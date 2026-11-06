from types import SimpleNamespace
from typing import Any

from english_agent.config import Settings
from english_agent.llm import AssistantTurn, ToolCall
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

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(usage=None, stop_reason="end_turn", content=[])


def _llm(effort: str | None) -> tuple[AnthropicLLM, _RecordingMessages]:
    llm = AnthropicLLM(Settings(_env_file=None, anthropic_api_key="k"), "claude-haiku-5-5", effort=effort)
    messages = _RecordingMessages()
    llm._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return llm, messages


def test_effort_goes_into_output_config() -> None:
    llm, messages = _llm("high")
    llm.chat([{"role": "user", "content": "hi"}], [])
    assert messages.kwargs["output_config"] == {"effort": "high"}


def test_no_effort_keeps_the_model_default() -> None:
    llm, messages = _llm(None)
    llm.chat([{"role": "user", "content": "hi"}], [])
    assert "output_config" not in messages.kwargs
