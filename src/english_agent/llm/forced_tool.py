"""One forced tool call that returns a validated pydantic model, with a single retry on failure.

Several stages of the pipeline do the same thing: force the model to call one submit tool whose
parameters are a pydantic schema (minus the fields code owns), validate the arguments, run an
optional semantic check, and feed any problem back once before giving up. `tool_schema` builds
the tool, `submit_model` runs the loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ValidationError

from english_agent.llm.base import ChatLLM
from english_agent.tracing import get_tracer


class LLMWriteError(Exception):
    """A stage could not get a valid structured output after its retries."""


def tool_schema(
    model: type[BaseModel],
    *,
    name: str,
    description: str,
    owned: tuple[str, ...] = (),
    required_sizes: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """A forced-tool definition from `model`'s JSON schema, without the `owned` (code-filled) fields.

    `required_sizes` pins a list field to an exact length and makes it required (used for fields that
    are optional on the stored model but mandatory from the writer).
    """
    schema = model.model_json_schema()
    for field in owned:
        schema["properties"].pop(field, None)
    schema["required"] = [item for item in schema.get("required", []) if item not in owned]
    for field, size in (required_sizes or {}).items():
        schema["properties"][field].pop("default", None)
        schema["properties"][field].update(minItems=size, maxItems=size)
        if field not in schema["required"]:
            schema["required"].append(field)
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


async def submit_model[T: BaseModel](
    llm: ChatLLM,
    model: type[T],
    *,
    tool: dict[str, Any],
    messages: list[dict[str, Any]],
    build: Callable[[dict[str, Any]], dict[str, Any]],
    problem: Callable[[T], str] = lambda _: "",
    span_name: str,
    span_attrs: Mapping[str, Any] | None = None,
    attempts: int = 2,
) -> T:
    """Force `tool`, validate its arguments into `model`, retry once with the error fed back.

    `build` turns the model's tool arguments into the full payload (adding the code-owned fields);
    `problem` returns "" for an acceptable result or a message to send back. `messages` is extended
    in place. Raises `LLMWriteError` when no attempt yields a valid, acceptable model.
    """
    name = tool["function"]["name"]
    tool_choice = {"type": "function", "function": {"name": name}}
    with get_tracer().start_as_current_span(span_name) as span:
        for key, value in (span_attrs or {}).items():
            span.set_attribute(key, value)
        last_error = f"model did not call {name}"
        for attempt in range(1, attempts + 1):
            turn = await llm.chat(messages, [tool], tool_choice=tool_choice)
            messages.append(turn.to_message())
            call = next((candidate for candidate in turn.tool_calls if candidate.name == name), None)
            if call is None:
                messages.append({"role": "user", "content": f"Call {name} with the result."})
                continue
            try:
                result = model.model_validate(build(call.arguments))
                last_error = problem(result)
                if not last_error:
                    span.set_attribute("llm.attempts", attempt)
                    return result
            except ValidationError as error:
                last_error = json.dumps(error.errors(include_url=False)[:5], ensure_ascii=False, default=str)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"Rejected, fix these problems and call {name} again: {last_error}",
                }
            )
        span.set_attribute("llm.attempts", attempts)
        span.set_status(Status(StatusCode.ERROR, last_error))
        raise LLMWriteError(f"{name}: no valid result after {attempts} attempts: {last_error}")
