"""The Diagnostician: why one learner keeps making a mistake, not only where.

"Errs in Present Perfect" is a symptom; the cause may be the rule (when the tense is needed), the
form (the third form of the verb) or transfer from Russian. The Diagnostician reads the learner's
errors and, with read-only tools, the answer history of an item and how the learner does on the
topic's prerequisites. It returns at most three hypotheses, each with the probe question that would
tell it from its neighbour and the intervention that fits, plus notes for the translation grader.

Code checks every hypothesis: its items are among the given ones and every cited error belongs to
one of its items; a rejected diagnosis goes back once with the problems, a second one fails the
run. The Diagnostician never grades and never changes the plan: it gives the Mentor material.
It grew out of the one-call Reflector and keeps its output (`Reflection`), which the grader, the
review Planner and the cheat sheet read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from opentelemetry.trace import Span
from pydantic import ValidationError

from english_agent.llm import ChatLLM, LLMWriteError, ToolCall, tool_schema
from english_agent.llm.agent_loop import Finish, ToolError, run_tool_loop
from english_agent.models.profile import Reflection
from english_agent.models.storage import ErrorRecord, EvidenceRecord, ItemAccuracy, ProfileInputs
from english_agent.topic_graph import TopicGraph
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "diagnose_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("based_on_errors", "until_error_id", "reflected_at", "provenance")
MAX_STEPS = 6
MAX_ERRORS = 40  # the newest errors it reads
MAX_HISTORY = 20  # answers per item_history call


class EvidenceReader(Protocol):
    """The read-only view of the learner's evidence the Diagnostician's tools use."""

    async def for_items(self, learner_id: int, item_ids: list[int], *, limit: int) -> list[EvidenceRecord]: ...

    async def topic_answers(self, learner_id: int, topic_ids: list[str]) -> dict[str, tuple[int, int]]: ...


def readable_errors(inputs: ProfileInputs) -> list[ErrorRecord]:
    """The newest errors that point at a known item, newest first."""
    known = [error for error in inputs.errors if error.item_id is not None and error.item_id in inputs.items]
    return sorted(known, key=lambda error: error.id, reverse=True)[:MAX_ERRORS]


def _tools() -> list[dict[str, Any]]:
    item = {"type": "object", "properties": {"item_key": {"type": "string"}}, "required": ["item_key"]}
    return [
        {
            "type": "function",
            "function": {
                "name": "list_errors",
                "description": "The learner's errors (optionally of one item): id, item, what they wrote, fix, type.",
                "parameters": {"type": "object", "properties": {"item_key": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "item_history",
                "description": "The learner's latest graded answers on one item, right and wrong, oldest first.",
                "parameters": item,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prerequisites",
                "description": "The topics the item's topic requires, and how the learner answers on each.",
                "parameters": item,
            },
        },
        tool_schema(
            Reflection,
            name="submit_diagnosis",
            description="Submit the diagnosis: up to three hypotheses and the notes for the grader. Ends the run.",
            owned=_OWNED_BY_CODE,
        ),
    ]


async def diagnose(
    llm: ChatLLM,
    *,
    learner_id: int | None,
    inputs: ProfileInputs,
    accuracy: Mapping[int, ItemAccuracy],
    previous: Reflection | None,
    now: datetime,
    reader: EvidenceReader | None,
    graph: TopicGraph,
    question: str | None = None,
) -> Reflection:
    """Diagnose the learner's errors (`question` narrows it, e.g. from the Mentor); raises on failure."""
    errors = readable_errors(inputs)
    if not errors:
        raise ValueError("nothing to diagnose: no errors with a known item")
    key_of = {error.id: inputs.items[error.item_id].key for error in errors if error.item_id is not None}
    by_key = {item.key: item for item in inputs.items.values()}
    provenance = build_provenance(
        component="diagnostician",
        schema_version=SCHEMA_VERSIONS["reflection"],
        model=getattr(llm, "model", None),
        prompt_names=("diagnose_system",),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": _brief(inputs, errors, accuracy, previous, question)},
    ]
    rejections = 0

    def problems(reflection: Reflection) -> str:
        found: list[str] = []
        for number, point in enumerate(reflection.focus_points):
            if unknown := sorted(set(point.item_keys) - set(key_of.values())):
                found.append(f"focus_points[{number}].item_keys not among the given items: {unknown}")
            for error_id in point.evidence_error_ids:
                if error_id not in key_of:
                    found.append(f"focus_points[{number}]: error id {error_id} is not one of the given errors")
                elif key_of[error_id] not in point.item_keys:
                    found.append(
                        f"focus_points[{number}]: error {error_id} belongs to {key_of[error_id]}, not its items"
                    )
            if point.cause is None or point.probe_ru is None or point.intervention is None:
                found.append(f"focus_points[{number}]: give cause, probe_ru and intervention")
        return "; ".join(found)

    def submit(arguments: dict[str, Any]) -> Reflection:
        nonlocal rejections
        payload = {
            **arguments,
            "based_on_errors": len(errors),
            "until_error_id": max(error.id for error in errors),
            "reflected_at": now,
            "provenance": provenance,
        }
        try:
            reflection = Reflection.model_validate(payload)
            problem = problems(reflection)
        except ValidationError as error:
            problem = json.dumps(error.errors(include_url=False)[:5], ensure_ascii=False, default=str)
        if not problem:
            return reflection
        rejections += 1
        if rejections >= 2:
            raise LLMWriteError(f"submit_diagnosis: rejected twice: {problem}")
        raise ToolError(f"Rejected, fix these problems and call submit_diagnosis again: {problem}")

    async def handle(call: ToolCall, step: int, span: Span) -> Any:
        arguments = call.arguments
        if call.name == "submit_diagnosis":
            return Finish(submit(arguments))
        if call.name == "list_errors":
            wanted = arguments.get("item_key")
            return [_error_line(error, key_of[error.id]) for error in errors if wanted in (None, key_of[error.id])]
        item_key = str(arguments.get("item_key", ""))
        item = by_key.get(item_key)
        if item is None:
            raise ToolError(f"unknown item_key {item_key!r}; use one of {sorted(by_key)}")
        if reader is None or learner_id is None:
            raise ToolError("the answer history is not available in this run; decide on the errors")
        if call.name == "item_history":
            history = await reader.for_items(learner_id, [item.id], limit=MAX_HISTORY)
            return [_answer_line(record) for record in history]
        if call.name == "prerequisites":
            if item.topic_id not in graph.nodes:
                return {"topic": item.topic_id, "prerequisites": []}
            required = list(graph.prerequisites(item.topic_id))
            answers = await reader.topic_answers(learner_id, required)
            return {
                "topic": item.topic_id,
                "prerequisites": [
                    {"topic": topic, "answers": answers.get(topic, (0, 0))[0], "correct": answers.get(topic, (0, 0))[1]}
                    for topic in required
                ],
            }
        raise ToolError(f"unknown tool {call.name!r}")

    result: Reflection | None
    result, steps = await run_tool_loop(
        llm,
        messages,
        _tools(),
        handle,
        max_steps=MAX_STEPS,
        nudge="Use a tool: list_errors, item_history, prerequisites, or submit_diagnosis.",
        step_attribute="diagnostician.step",
    )
    if result is None:
        raise LLMWriteError(f"diagnostician: no diagnosis after {steps} steps")
    return result


def _error_line(error: ErrorRecord, item_key: str) -> dict[str, Any]:
    return {
        "id": error.id,
        "item": item_key,
        "wrote": error.original,
        "correct": error.corrected,
        "type": error.error_type,
        "at": f"{error.created_at:%Y-%m-%d}",
    }


def _answer_line(record: EvidenceRecord) -> dict[str, Any]:
    payload = record.payload
    correct = (
        payload.get("is_correct") if record.kind != "translation_sentence" else payload.get("verdict") == "correct"
    )
    return {"at": f"{record.occurred_at:%Y-%m-%d}", "kind": record.kind, "correct": bool(correct)}


def _brief(
    inputs: ProfileInputs,
    errors: list[ErrorRecord],
    accuracy: Mapping[int, ItemAccuracy],
    previous: Reflection | None,
    question: str | None,
) -> str:
    lines = [f"Level: {inputs.cefr}."]
    if question:
        lines.append(f"The tutor asks: {question}")
    lines += ["", "Items in the errors:"]
    for item in sorted({error.item_id for error in errors if error.item_id is not None}):
        record = inputs.items[item]
        known = accuracy.get(item)
        rate = f", accuracy {known.correct}/{known.checks}" if known is not None and known.checks else ""
        lines.append(f"- {record.key} (topic {record.topic_id}): {record.form} — {record.function_ru}{rate}")
    lines.append(f"{len(errors)} errors are available through list_errors.")
    if previous is not None:
        kept = previous.model_dump(mode="json", include={"focus_points", "notes_ru"})
        lines.append(f"Previous diagnosis ({previous.reflected_at:%Y-%m-%d}): {json.dumps(kept, ensure_ascii=False)}")
    return "\n".join(lines)
