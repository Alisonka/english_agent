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


# --- step-based topic content (models/topic.py) ---------------------------------------

SOURCE: dict[str, Any] = {
    "url": "https://skysmart.ru/articles/english/konstrukciya-there-there-are",
    "title": "There is / are",
    "accessed_at": "2026-10-16",
}

STEP_MESSAGE_SINGULAR: dict[str, Any] = {
    "hook_ru": "Как сказать «на столе книга», если по-английски без глагола нельзя?",
    "key_idea_ru": "Там, где по-русски нет глагола, по-английски для одного предмета ставим there is.",
    "form": "There is + существительное в единственном числе",
    "examples": [
        {"en": "There is a cat here.", "ru": "Здесь кот.", "highlights": ["There is"]},
        {"en": "There is a book on the table.", "ru": "На столе книга.", "highlights": ["There is"]},
        {"en": "There is some milk.", "ru": "Есть немного молока.", "highlights": ["There is"]},
    ],
    "incorrect": "It is a cat here.",
    "correct": "There is a cat here.",
    "contrast_ru": "Русское «это кот» — It is a cat; «тут есть кот» — There is a cat.",
    "self_check": {
        "prompt_en": "Say in English: На столе лампа.",
        "answer_en": "There is a lamp on the table.",
        "explanation_ru": "Один предмет → there is.",
    },
    "teaser_ru": "Завтра: как сказать про много предметов и как задать вопрос.",
}

STEP_MESSAGE_PLURAL: dict[str, Any] = {
    "hook_ru": "А если предметов много или нужно спросить, есть ли что-то рядом?",
    "key_idea_ru": "Для многих предметов — there are, а в вопросе there выходит вперёд: Is there…?",
    "form": "There are + множественное число; Is there…? / Are there…?",
    "examples": [
        {"en": "There are two books.", "ru": "Есть две книги.", "highlights": ["There are"]},
        {"en": "Is there a bank here?", "ru": "Здесь есть банк?", "highlights": ["Is there"]},
        {"en": "There aren't any shops.", "ru": "Магазинов нет.", "highlights": ["aren't"]},
    ],
    "incorrect": "There is two books.",
    "correct": "There are two books.",
    "contrast_ru": "Один предмет — there is, несколько — there are; в вопросе глагол впереди.",
    "self_check": {
        "prompt_en": "Ask in English: Здесь есть кафе?",
        "answer_en": "Is there a cafe here?",
        "explanation_ru": "Вопрос → Is there…?",
    },
    "teaser_ru": None,
}


def step_quiz(slug: str) -> list[dict[str, Any]]:
    """Five valid quiz questions tagged with `slug` for a step practice bank."""
    base: list[dict[str, Any]] = [
        {
            "kind": "gap_fill",
            "question": "___ a cat in the kitchen.",
            "options": ["There is", "There are", "It is", "They are"],
            "correct_option": 0,
            "explanation_ru": "Один предмет — there is.",
        },
        {
            "kind": "gap_fill",
            "question": "___ two cats in the kitchen.",
            "options": ["There is", "There are", "It are", "They is"],
            "correct_option": 1,
            "explanation_ru": "Много предметов — there are.",
        },
        {
            "kind": "choose_translation",
            "question": "Здесь есть банк?",
            "options": ["Is there a bank here?", "Is it a bank here?", "There is a bank here?"],
            "correct_option": 0,
            "explanation_ru": "Вопрос о наличии — Is there…?",
        },
        {
            "kind": "contrast",
            "question": "___ some milk in the fridge.",
            "options": ["There is", "There are", "It is"],
            "correct_option": 0,
            "explanation_ru": "Молоко неисчисляемое — there is.",
        },
        {
            "kind": "choose_translation",
            "question": "На столе две книги.",
            "options": [
                "There are two books on the table.",
                "There is two books on the table.",
                "It is two books on the table.",
            ],
            "correct_option": 0,
            "explanation_ru": "Множественное число — there are.",
        },
    ]
    return [{**question, "item_slug": slug} for question in base]


def step_translation(slug: str) -> list[dict[str, Any]]:
    """Three valid translation sentences tagged with `slug` for a step practice bank."""
    base: list[dict[str, Any]] = [
        {"ru": "На столе книга.", "reference_en": "There is a book on the table.", "focus_ru": "there is + ед. ч."},
        {"ru": "В парке нет детей.", "reference_en": "There aren't any children in the park.", "focus_ru": "отрицание"},
        {"ru": "Здесь есть кафе?", "reference_en": "Is there a cafe here?", "focus_ru": "вопрос Is there"},
    ]
    return [{**sentence, "item_slug": slug} for sentence in base]
