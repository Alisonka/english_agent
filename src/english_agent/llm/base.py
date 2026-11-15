"""Provider-neutral chat types. Agent code depends only on these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Tokens one call used and its estimated cost (None when the model has no price)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-native content blocks (e.g. Anthropic thinking + tool_use blocks with
    # signatures). When present they are replayed verbatim so multi-turn tool use works.
    raw_blocks: list[dict[str, Any]] | None = None
    usage: Usage | None = None  # None when the provider reported none (and for scripted turns)

    def to_message(self) -> dict[str, Any]:
        if self.raw_blocks is not None:
            return {"role": "assistant", "content": self.raw_blocks}
        blocks: list[dict[str, Any]] = []
        if self.content:
            blocks.append({"type": "text", "text": self.content})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in self.tool_calls
        )
        return {"role": "assistant", "content": blocks}


class LLMError(Exception):
    """A model call failed; see the subclasses for whether retrying can help."""


class LLMUnavailable(LLMError):
    """Rate limited, overloaded, a 5xx, a timeout or a network error after the SDK's own retries.

    Transient: the job that made the call should be retried later, with backoff.
    """


class LLMRejected(LLMError):
    """The request itself was refused (400/401/403/404): retrying the same request will not help."""


class ChatLLM(Protocol):
    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn: ...
