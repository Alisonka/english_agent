"""The Reflector: what keeps going wrong for one learner, read from their recent errors.

It runs inside the profile refresh (after a finished quiz or a graded translation), at most once
every `MIN_GAP` per learner and only when there are new errors to read (`reflection_due`): enough
of them, or one after a long quiet stretch. A model (REFLECTOR_MODEL) groups the errors into up to
three focus points; code checks that every point names items from the input and rests on errors
of those items, and owns the bookkeeping fields. The result is the profile's `reflection`, which
the grader (explanations), the Planner (review slots) and the cheat sheet (hints) read while it is
current. A failed reflection is logged and the previous one is kept.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.llm import AnthropicLLM, ChatLLM, submit_model, tool_schema
from english_agent.models.profile import Reflection
from english_agent.models.storage import ErrorRecord, ItemAccuracy, ProfileInputs
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "reflect_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("based_on_errors", "until_error_id", "reflected_at", "provenance")

MIN_GAP = timedelta(hours=20)  # at most about one reflection a day per learner
QUIET_GAP = timedelta(days=7)  # after this long, even one new error is worth a fresh look
NEW_ERRORS = 3  # new errors that call for a reflection sooner
MAX_ERRORS = 40  # the newest errors the Reflector reads


@dataclass(frozen=True)
class ReflectionRequest:
    """What one reflection reads: the profile's window of errors and items, accuracy, the previous reflection."""

    inputs: ProfileInputs
    accuracy: dict[int, ItemAccuracy]
    previous: Reflection | None
    now: datetime


Reflector = Callable[[ReflectionRequest], Awaitable[Reflection]]


def reflection_due(previous: Reflection | None, errors: list[ErrorRecord], now: datetime) -> bool:
    """Whether the errors since the last reflection are worth a new one."""
    if previous is not None and now - previous.reflected_at < MIN_GAP:
        return False
    new = [
        error
        for error in errors
        if error.item_id is not None and (previous is None or error.id > previous.until_error_id)
    ]
    if len(new) >= NEW_ERRORS:
        return True
    return previous is not None and bool(new) and now - previous.reflected_at >= QUIET_GAP


def _readable(inputs: ProfileInputs) -> list[ErrorRecord]:
    """The newest errors that point at a known item, newest first."""
    known = [error for error in inputs.errors if error.item_id is not None and error.item_id in inputs.items]
    return sorted(known, key=lambda error: error.id, reverse=True)[:MAX_ERRORS]


async def reflect(llm: ChatLLM, request: ReflectionRequest) -> Reflection:
    inputs = request.inputs
    errors = _readable(inputs)
    if not errors:
        raise ValueError("nothing to reflect on: no errors with a known item")
    key_of = {error.id: inputs.items[error.item_id].key for error in errors if error.item_id is not None}
    known_keys = set(key_of.values())
    provenance = build_provenance(
        component="reflector",
        schema_version=SCHEMA_VERSIONS["reflection"],
        model=getattr(llm, "model", None),
        prompt_names=("reflect_system",),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": _brief(request, errors)},
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            **arguments,
            "based_on_errors": len(errors),
            "until_error_id": max(error.id for error in errors),
            "reflected_at": request.now,
            "provenance": provenance,
        }

    def problem(reflection: Reflection) -> str:
        problems: list[str] = []
        for number, point in enumerate(reflection.focus_points):
            if unknown := sorted(set(point.item_keys) - known_keys):
                problems.append(f"focus_points[{number}].item_keys not among the given items: {unknown}")
            for error_id in point.evidence_error_ids:
                if error_id not in key_of:
                    problems.append(f"focus_points[{number}]: error id {error_id} is not one of the given errors")
                elif key_of[error_id] not in point.item_keys:
                    problems.append(
                        f"focus_points[{number}]: error {error_id} belongs to {key_of[error_id]}, not to its items"
                    )
        return "; ".join(problems)

    return await submit_model(
        llm,
        Reflection,
        tool=tool_schema(
            Reflection,
            name="submit_reflection",
            description="Submit what keeps going wrong for this learner: the focus points and the notes.",
            owned=_OWNED_BY_CODE,
        ),
        messages=messages,
        build=build,
        problem=problem,
        span_name="reflector.reflect",
        span_attrs={"reflector.errors": len(errors)},
    )


def _brief(request: ReflectionRequest, errors: list[ErrorRecord]) -> str:
    inputs = request.inputs
    lines = [f"Level: {inputs.cefr}.", "", "Errors, newest first:"]
    for error in errors:
        assert error.item_id is not None
        item = inputs.items[error.item_id]
        lines.append(
            f"[{error.id}] item {item.key} (form: {item.form}; for: {item.function_ru}) — {error.kind}, "
            f"{error.created_at:%Y-%m-%d}: {error.original!r} -> {error.corrected!r}"
            + (f", type {error.error_type}" if error.error_type else "")
            + (f". Explained: {error.explanation_ru}" if error.explanation_ru else "")
        )
    lines.append("")
    lines.append("Accuracy on these items (correct / checked, over days):")
    for item_id, item in sorted(inputs.items.items(), key=lambda pair: pair[1].key):
        accuracy = request.accuracy.get(item_id)
        if accuracy is not None:
            lines.append(f"- {item.key}: {accuracy.correct}/{accuracy.checks} over {accuracy.days} day(s)")
    previous = request.previous
    lines.append("")
    if previous is None:
        lines.append("Previous reflection: none.")
    else:
        kept = previous.model_dump(mode="json", include={"focus_points", "notes_ru"})
        lines.append(f"Previous reflection ({previous.reflected_at:%Y-%m-%d}): {json.dumps(kept, ensure_ascii=False)}")
    return "\n".join(lines)


def make_reflector(settings: Settings, client: AsyncAnthropic | None = None) -> Reflector | None:
    """Reflect with REFLECTOR_MODEL through the process's Claude client; None when the Reflector is off."""
    if not settings.reflector_model:
        return None
    llm = AnthropicLLM(settings, model=settings.reflector_model, effort=settings.reflector_effort, client=client)

    async def run(request: ReflectionRequest) -> Reflection:
        return await reflect(llm, request)

    return run
