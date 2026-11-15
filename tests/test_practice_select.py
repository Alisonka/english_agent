from __future__ import annotations

import random

from conftest import lesson_payload, topic_content_payload
from english_agent.models.lesson import Lesson
from english_agent.models.practice import DAILY_QUIZ_SIZE, TRANSLATION_SIZE, QuizQuestion, TranslationTask
from english_agent.models.topic import TopicContent
from english_agent.practice_select import (
    StepBank,
    assemble_quiz,
    assemble_translation,
    bank_from_lesson,
    bank_from_step,
    choose_previous,
)


def _q(text: str) -> QuizQuestion:
    return QuizQuestion(
        kind="gap_fill",
        question=f"Question {text} here",
        options=["a", "b", "c"],
        correct_option=0,
        explanation_ru="потому что так надо",
    )


def _s(text: str) -> TranslationTask:
    return TranslationTask(ru=f"Предложение про {text}", reference_en="There is a thing.", focus_ru="проверка формы")


def _bank(item_id: int, key: str, prefix: str) -> StepBank:
    return StepBank(
        item_id=item_id,
        item_key=key,
        title_en=key,
        key_idea_ru="идея",
        form=None,
        questions=[_q(f"{prefix}{i}") for i in range(5)],
        sentences=[_s(f"{prefix}{i}") for i in range(3)],
    )


def test_quiz_has_two_today_one_previous_one_review() -> None:
    block = assemble_quiz(_bank(1, "t:a", "x"), _bank(2, "t:b", "y"), _bank(3, "o:c", "z"), rng=random.Random(0))
    assert len(block.questions) == DAILY_QUIZ_SIZE
    assert [q.source for q in block.questions] == ["today", "today", "previous", "review"]
    assert {q.item_key for q in block.questions} == {"t:a", "t:b", "o:c"}


def test_quiz_day_one_without_previous_still_has_four() -> None:
    block = assemble_quiz(_bank(1, "t:a", "x"), None, _bank(3, "o:c", "z"), rng=random.Random(1))
    assert len(block.questions) == DAILY_QUIZ_SIZE
    assert [q.source for q in block.questions].count("review") >= 1


def test_quiz_without_any_cards_falls_back_to_today() -> None:
    block = assemble_quiz(_bank(1, "t:a", "x"), None, None, rng=random.Random(2))
    assert len(block.questions) == DAILY_QUIZ_SIZE
    assert {q.source for q in block.questions} == {"today"}


def test_quiz_prefers_unasked_questions() -> None:
    today = _bank(1, "t:a", "x")
    asked = {"t:a": {q.question for q in today.questions[:3]}}  # only 2 remain unasked
    block = assemble_quiz(today, None, None, asked=asked, rng=random.Random(3))
    today_texts = [q.question.question for q in block.questions if q.source == "today"]
    unasked = [text for text in today_texts if text not in asked["t:a"]]
    assert len(unasked) == 2  # both unasked questions were chosen before any repeat


def test_translation_has_two_today_one_other() -> None:
    block = assemble_translation(
        _bank(1, "t:a", "x"), _bank(2, "t:b", "y"), other_source="review", rng=random.Random(0)
    )
    assert len(block.sentences) == TRANSLATION_SIZE
    assert [s.source for s in block.sentences] == ["today", "today", "review"]


def test_translation_without_other_fills_from_today() -> None:
    block = assemble_translation(_bank(1, "t:a", "x"), None, rng=random.Random(0))
    assert len(block.sentences) == TRANSLATION_SIZE
    assert {s.source for s in block.sentences} == {"today"}


def test_choose_previous_picks_the_least_recalled() -> None:
    banks = [_bank(2, "t:b", "y"), _bank(3, "t:c", "z")]
    assert choose_previous(banks, {2: 0.9, 3: 0.2}).item_key == "t:c"
    assert choose_previous(banks, {}).item_key == "t:b"  # unseen items tie at 0, first wins
    assert choose_previous([], {}) is None


def test_bank_from_step_drops_the_item_slug() -> None:
    content = TopicContent.model_validate(topic_content_payload())
    bank = bank_from_step(content.step(1), item_id=7)
    assert bank.item_key == "there_is_are:singular" and bank.item_id == 7
    assert len(bank.questions) == 5 and len(bank.sentences) == 3
    assert all(isinstance(question, QuizQuestion) for question in bank.questions)


def test_bank_from_lesson_uses_the_core_item() -> None:
    lesson = Lesson.model_validate(lesson_payload())
    bank = bank_from_lesson(lesson, "there_is_are:core", item_id=4)
    assert bank.item_key == "there_is_are:core" and bank.item_id == 4
    assert len(bank.questions) == 3 and len(bank.sentences) == 3
