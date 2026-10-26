from __future__ import annotations

from typing import Any

import pytest

from english_agent.models.lesson import Lesson


def lesson_payload(**overrides: Any) -> dict[str, Any]:
    """A valid `Lesson` payload; pass overrides to tweak individual fields."""
    payload: dict[str, Any] = {
        "topic_id": "there_is_are",
        "title_en": "there is / there are",
        "title_ru": "Конструкция there is / there are",
        "cefr": "A2",
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
            {"en": "There is a cat in the kitchen.", "ru": "На кухне кот.", "highlights": ["There is"]},
            {"en": "There are two books on the table.", "ru": "На столе две книги.", "highlights": ["There are"]},
            {"en": "Is there a bank near here?", "ru": "Здесь рядом есть банк?", "highlights": ["Is there"]},
            {"en": "There isn't any milk.", "ru": "Молока нет.", "highlights": ["There isn't"]},
        ],
        "mistake_ru": "Калька с русского «это есть кот» даёт It is a cat вместо There is.",
        "incorrect": "It is a cat in the kitchen.",
        "correct": "There is a cat in the kitchen.",
        "sources": [
            {
                "url": "https://skysmart.ru/articles/english/konstrukciya-there-there-are",
                "title": "There is / are",
                "accessed_at": "2026-10-16",
            }
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def lesson() -> Lesson:
    return Lesson.model_validate(lesson_payload())
