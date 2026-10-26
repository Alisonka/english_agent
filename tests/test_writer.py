from __future__ import annotations

from typing import Any

import pytest

from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.curriculum import CurriculumTopic
from english_agent.research.models import TopicNotes
from english_agent.writer import LessonWriteError, write_lesson

TOPIC = CurriculumTopic(
    id="there_is_are",
    title_en="there is / there are",
    title_ru="there is / there are",
    order=13,
    tags=["there"],
)

NOTES = TopicNotes(
    site="skysmart",
    source={
        "url": "https://skysmart.ru/articles/english/konstrukciya-there-there-are",
        "title": "There is / are",
        "accessed_at": "2026-10-16",
    },
    page_title="There is / are",
    summary_ru="Статья объясняет конструкцию there is / there are для описания того, что где-то находится.",
    rule_points=[
        "there is + единственное число",
        "there are + множественное",
        "вопрос: Is there / Are there",
    ],
    examples=[
        {"en": "There is a cat in the kitchen.", "ru": "На кухне кот."},
        {"en": "There are two books on the table."},
    ],
)

GOOD_ARGS: dict[str, Any] = {
    "title_en": "there is / there are",
    "title_ru": "Конструкция there is / there are",
    "key_idea_ru": "Там, где по-русски «на столе книга» без глагола, по-английски нужен оборот there is / there are.",
    "rule_ru": (
        "Конструкция there is / there are говорит, что где-то что-то есть или находится. There is "
        "используем с единственным числом и неисчисляемыми: There is a cat in the kitchen. There are — "
        "с множественным: There are two books on the table. В отрицании добавляем not: There isn't a bank "
        "here. В вопросе глагол выходит вперёд: Is there a bank near here? Отвечаем коротко: Yes, there is. "
        "No, there isn't. Переводим обычно с конца предложения, начиная с места."
    ),
    "form": "There is + singular noun / There are + plural noun",
    "nuances_ru": [
        "Если перечисляем несколько предметов, глагол согласуется с первым: There is a table and two chairs.",
        "С неисчисляемыми всегда there is: There is some milk in the fridge.",
        "There здесь не значит «там»; место указывается отдельно в конце: There is a shop there.",
    ],
    "examples": [
        {
            "en": "There is a cat in the kitchen.",
            "ru": "На кухне кот.",
            "highlights": ["There is"],
        },
        {
            "en": "There are two books on the table.",
            "ru": "На столе две книги.",
            "highlights": ["There are"],
        },
        {
            "en": "Is there a bank near here?",
            "ru": "Здесь рядом есть банк?",
            "highlights": ["Is there"],
        },
        {
            "en": "There isn't any milk.",
            "ru": "Молока нет.",
            "highlights": ["There isn't"],
        },
    ],
    "mistake_ru": "Калька с русского «это есть кот» даёт It is a cat вместо There is.",
    "incorrect": "It is a cat in the kitchen.",
    "correct": "There is a cat in the kitchen.",
}


class ScriptedLLM:
    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Any = "auto",
    ) -> AssistantTurn:
        self.calls.append({"messages": list(messages), "tools": tools, "tool_choice": tool_choice})
        return self.turns.pop(0)


def submit(args: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="submit_lesson", arguments=args)],
    )


def test_writer_returns_lesson_with_code_owned_provenance() -> None:
    llm = ScriptedLLM(
        [
            submit(
                {
                    **GOOD_ARGS,
                    "topic_id": "wrong",
                    "cefr": "B2",
                    "sources": [
                        {
                            "url": "https://evil.example/",
                            "title": "x",
                            "accessed_at": "2020-01-01",
                        }
                    ],
                }
            )
        ]
    )
    lesson = write_lesson(llm, TOPIC, NOTES)

    assert lesson.topic_id == "there_is_are"
    assert lesson.cefr == "A2"
    assert [str(s.url) for s in lesson.sources] == [str(NOTES.source.url)]
    assert lesson.provenance is not None
    assert lesson.provenance.component == "writer"
    assert "write_lesson_system" in lesson.provenance.prompts
    assert llm.calls[0]["tool_choice"]["function"]["name"] == "submit_lesson"
    params = llm.calls[0]["tools"][0]["function"]["parameters"]
    assert not {"topic_id", "cefr", "sources"} & set(params["properties"])


def test_writer_feeds_validation_errors_back_once() -> None:
    bad = {
        **GOOD_ARGS,
        "examples": [{"en": "There is a cat.", "ru": "Кот.", "highlights": ["There are"]}],
    }
    llm = ScriptedLLM([submit(bad), submit(GOOD_ARGS)])
    lesson = write_lesson(llm, TOPIC, NOTES)
    assert len(lesson.examples) == 4
    retry_messages = llm.calls[1]["messages"]
    assert retry_messages[-1]["role"] == "tool"
    assert "highlights" in retry_messages[-1]["content"]


def test_writer_gives_up_after_attempts() -> None:
    bad = {**GOOD_ARGS, "rule_ru": "коротко"}
    llm = ScriptedLLM([submit(bad), submit(bad)])
    with pytest.raises(LessonWriteError):
        write_lesson(llm, TOPIC, NOTES)
