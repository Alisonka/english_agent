"""write_lesson: TopicNotes in, validated Lesson out.

One forced tool call. The model fills the pedagogical fields; the code owns provenance
(`topic_id`, `cefr`, `sources`) so the lesson can never cite a page the research step did not
actually read. A validation failure is fed back to the model once; a second failure raises.

Legacy: superseded by the step-based generation (`english_agent.topic_writer`); kept while
stored lessons of schema v3 are still served and graded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from english_agent.llm import ChatLLM, LLMWriteError, submit_model, tool_schema
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QUIZ_SIZE, TRANSLATION_SIZE
from english_agent.research.models import TopicNotes
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "write_lesson_system.md").read_text(encoding="utf-8")
_OWNED_BY_CODE = ("topic_id", "cefr", "sources", "provenance")


class LessonWriteError(LLMWriteError):
    pass


def _submit_tool() -> dict[str, Any]:
    return tool_schema(
        Lesson,
        name="submit_lesson",
        description="Submit the finished lesson.",
        owned=_OWNED_BY_CODE,
        required_sizes={"quiz": QUIZ_SIZE, "translation": TRANSLATION_SIZE},
    )


def _practice_problem(lesson: Lesson) -> str:
    """Why the lesson's practice is incomplete, or "" when it is complete."""
    problems: list[str] = []
    if len(lesson.quiz) != QUIZ_SIZE:
        problems.append(f"quiz must have exactly {QUIZ_SIZE} questions, got {len(lesson.quiz)}")
    if len(lesson.translation) != TRANSLATION_SIZE:
        problems.append(f"translation must have exactly {TRANSLATION_SIZE} sentences, got {len(lesson.translation)}")
    return "; ".join(problems)


async def write_lesson(
    llm: ChatLLM,
    topic: CurriculumTopic,
    notes: TopicNotes,
    *,
    cefr: Cefr = "A2",
    attempts: int = 2,
    curriculum_version: int | None = None,
) -> Lesson:
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

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            **arguments,
            "topic_id": topic.id,
            "cefr": cefr,
            "sources": [notes.source.model_dump()],
            "provenance": provenance,
        }

    try:
        return await submit_model(
            llm,
            Lesson,
            tool=_submit_tool(),
            messages=messages,
            build=build,
            problem=_practice_problem,
            span_name="writer.write_lesson",
            span_attrs={"topic.id": topic.id},
            attempts=attempts,
        )
    except LLMWriteError as error:
        raise LessonWriteError(str(error)) from error
