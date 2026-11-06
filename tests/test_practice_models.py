from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conftest import lesson_payload
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion
from samples import QUIZ


def question(**overrides: Any) -> dict[str, Any]:
    return {**QUIZ[0], **overrides}


def test_sample_questions_are_valid() -> None:
    assert [QuizQuestion.model_validate(item).kind for item in QUIZ] == ["gap_fill", "choose_translation", "contrast"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"correct_option": 4},
        {"options": ["There is", "there is ", "It is"]},
        {"options": ["There is", "There are"]},
        {"options": ["a", "b", "c", "d", "e"]},
        {"explanation_ru": "Строка один.\nДва.\nТри.\nЧетыре."},
        {"explanation_ru": "x" * 201},
        {"question": "q" * 301},
        {"options": ["x" * 101, "There are", "It is"]},
    ],
)
def test_question_rejects_what_telegram_or_logic_would_reject(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QuizQuestion.model_validate(question(**overrides))


def test_lesson_without_practice_still_loads() -> None:
    """Lessons stored before schema v3 have no quiz/translation."""
    payload = lesson_payload()
    del payload["quiz"], payload["translation"]
    lesson = Lesson.model_validate(payload)
    assert lesson.quiz == [] and lesson.translation == []
