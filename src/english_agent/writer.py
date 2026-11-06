"""write_lesson: TopicNotes in, validated Lesson out.

One forced tool call. The model fills the pedagogical fields; the code owns
provenance (`topic_id`, `cefr`, `sources`) so the lesson can never cite a page
the research step did not actually read. A validation failure is fed back to the
model once; a second failure raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from english_agent.llm import ChatLLM
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QUIZ_SIZE, TRANSLATION_SIZE
from english_agent.research.models import TopicNotes
from english_agent.tracing import get_tracer
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "write_lesson_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("topic_id", "cefr", "sources", "provenance")


class LessonWriteError(Exception):
    pass


def _submit_tool() -> dict[str, Any]:
    schema = Lesson.model_json_schema()
    for field in _OWNED_BY_CODE:
        schema["properties"].pop(field, None)
    schema["required"] = [name for name in schema.get("required", []) if name not in _OWNED_BY_CODE]
    # Optional in the model (old stored lessons have none), required from the writer.
    for field, size in (("quiz", QUIZ_SIZE), ("translation", TRANSLATION_SIZE)):
        schema["properties"][field].pop("default", None)
        schema["properties"][field].update(minItems=size, maxItems=size)
        schema["required"].append(field)
    return {
        "type": "function",
        "function": {
            "name": "submit_lesson",
            "description": "Submit the finished lesson.",
            "parameters": schema,
        },
    }


def _practice_problem(lesson: Lesson) -> str:
    """Why the lesson's practice is incomplete, or "" when it is complete."""
    problems: list[str] = []
    if len(lesson.quiz) != QUIZ_SIZE:
        problems.append(f"quiz must have exactly {QUIZ_SIZE} questions, got {len(lesson.quiz)}")
    if len(lesson.translation) != TRANSLATION_SIZE:
        problems.append(f"translation must have exactly {TRANSLATION_SIZE} sentences, got {len(lesson.translation)}")
    return "; ".join(problems)


def write_lesson(
    llm: ChatLLM,
    topic: CurriculumTopic,
    notes: TopicNotes,
    *,
    cefr: Cefr = "A2",
    attempts: int = 2,
    curriculum_version: int | None = None,
) -> Lesson:
    tool = _submit_tool()
    tool_choice = {"type": "function", "function": {"name": "submit_lesson"}}
    provenance = build_provenance(
        component="writer",
        schema_version=SCHEMA_VERSIONS["lesson"],
        model=getattr(llm, "model", None),
        prompt_names=("write_lesson_system",),
        curriculum_version=curriculum_version,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {
            "role": "user",
            "content": (
                f"Topic: {topic.title_en} / {topic.title_ru} (id: {topic.id}, level {cefr})\n\n"
                f"Research notes (JSON):\n{notes.model_dump_json(indent=2, ensure_ascii=False)}"
            ),
        },
    ]

    with get_tracer().start_as_current_span("writer.write_lesson") as span:
        span.set_attribute("topic.id", topic.id)
        last_error = "model did not call submit_lesson"
        for attempt in range(1, attempts + 1):
            turn = llm.chat(messages, [tool], tool_choice=tool_choice)
            messages.append(turn.to_message())
            call = next((c for c in turn.tool_calls if c.name == "submit_lesson"), None)
            if call is None:
                messages.append({"role": "user", "content": "Call submit_lesson with the lesson."})
                continue

            payload = {
                **call.arguments,
                "topic_id": topic.id,
                "cefr": cefr,
                "sources": [notes.source.model_dump()],
                "provenance": provenance,
            }
            try:
                lesson = Lesson.model_validate(payload)
                last_error = _practice_problem(lesson)
                if not last_error:
                    span.set_attribute("writer.attempts", attempt)
                    return lesson
            except ValidationError as error:
                last_error = json.dumps(error.errors(include_url=False)[:5], ensure_ascii=False, default=str)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"Lesson rejected, fix these problems and call submit_lesson again: {last_error}",
                }
            )

        span.set_attribute("writer.attempts", attempts)
        span.set_status(Status(StatusCode.ERROR, last_error))
        raise LessonWriteError(f"could not produce a valid lesson after {attempts} attempts: {last_error}")
