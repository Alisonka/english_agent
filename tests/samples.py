"""Valid practice payloads for the `there_is_are` sample lesson, shared by several test modules."""

from __future__ import annotations

from typing import Any

QUIZ: list[dict[str, Any]] = [
    {
        "kind": "gap_fill",
        "question": "___ two cats in the garden.",
        "options": ["There is", "There are", "It is", "They are"],
        "correct_option": 1,
        "explanation_ru": "Два кота — множественное число, поэтому there are.",
    },
    {
        "kind": "choose_translation",
        "question": "В холодильнике есть молоко.",
        "options": ["It is milk in the fridge.", "There is milk in the fridge.", "There are milk in the fridge."],
        "correct_option": 1,
        "explanation_ru": "Молоко неисчисляемое, поэтому there is; it is здесь не подходит.",
    },
    {
        "kind": "contrast",
        "question": "___ a bank near here? — Yes, there is.",
        "options": ["Is it", "Is there", "Are there"],
        "correct_option": 1,
        "explanation_ru": "Вопрос о наличии: Is there…? Is it…? спрашивает, что это за предмет.",
    },
]

TRANSLATION: list[dict[str, Any]] = [
    {
        "ru": "В моей комнате есть окно.",
        "reference_en": "There is a window in my room.",
        "focus_ru": "there is + единственное число",
    },
    {
        "ru": "В парке нет детей.",
        "reference_en": "There aren't any children in the park.",
        "focus_ru": "отрицание there aren't",
    },
    {
        "ru": "Здесь есть кафе?",
        "reference_en": "Is there a cafe here?",
        "focus_ru": "вопрос Is there…?",
    },
]
