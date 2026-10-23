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
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from english_agent.config import Settings
from english_agent.llm.base import AssistantTurn, ToolCall


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
    def __init__(self, settings: Settings, model: str | None = None) -> None:
        if not settings.anthropic_api_key.get_secret_value():
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.model = model or settings.research_model
        self.max_tokens = settings.llm_max_tokens
        self._client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn:
        system, converted = to_anthropic_messages(messages)
        response = self._client.messages.create(  # type: ignore[call-overload]
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=converted,
            tools=to_anthropic_tools(tools),
            tool_choice=to_anthropic_tool_choice(tool_choice),
        )
        blocks = [json.loads(block.model_dump_json(exclude_none=True)) for block in response.content]
        return parse_response_blocks(blocks)
