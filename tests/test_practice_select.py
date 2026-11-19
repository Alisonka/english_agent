from __future__ import annotations

import random

from conftest import lesson_payload, topic_content_payload
from english_agent.models.lesson import Lesson
from english_agent.models.practice import DAILY_QUIZ_SIZE, TRANSLATION_SIZE, QuizQuestion, TranslationTask
from english_agent.models.storage import AskedHistory, PlanReason
from english_agent.models.topic import TopicContent
from english_agent.planner import Candidate, Pick
from english_agent.practice_select import (
    StepBank,
    TaggedQuestion,
    assemble_quiz,
    assemble_translation,
    bank_from_lesson,
    bank_from_step,
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
        questions=[TaggedQuestion(_q(f"{prefix}{i}"), item_id, key) for i in range(5)],
        sentences=[_s(f"{prefix}{i}") for i in range(3)],
    )


def _pick(bank: StepBank, reason: PlanReason, *, same_topic: bool) -> tuple[Pick, StepBank]:
    assert bank.item_id is not None
    candidate = Candidate(item_id=bank.item_id, item_key=bank.item_key, same_topic=same_topic)
    return Pick(candidate, reason), bank


def test_quiz_has_two_today_one_previous_and_one_review() -> None:
    picks = [
        _pick(_bank(2, "t:b", "y"), "previous", same_topic=True),
        _pick(_bank(3, "o:c", "z"), "due", same_topic=False),
    ]
    block = assemble_quiz(_bank(1, "t:a", "x"), picks, rng=random.Random(0))
    assert [(q.source, q.reason) for q in block.questions] == [
        ("today", "today"),
        ("today", "today"),
        ("previous", "previous"),
        ("review", "due"),
    ]
    assert len({q.question.question for q in block.questions}) == DAILY_QUIZ_SIZE


def test_a_learner_behind_on_reviews_gets_a_longer_quiz() -> None:
    reviews = [_pick(_bank(10 + i, f"o:{i}", f"r{i}"), "due", same_topic=False) for i in range(3)]
    block = assemble_quiz(_bank(1, "t:a", "x"), reviews, rng=random.Random(1))
    assert len(block.questions) == 5
    assert [q.item_key for q in block.questions[2:]] == ["o:0", "o:1", "o:2"]


def test_a_short_plan_is_filled_from_todays_bank() -> None:
    block = assemble_quiz(_bank(1, "t:a", "x"), [], rng=random.Random(2))
    assert len(block.questions) == DAILY_QUIZ_SIZE
    assert {q.source for q in block.questions} == {"today"}


def test_a_review_asks_only_its_own_items_questions() -> None:
    foreign = StepBank(
        item_id=7,
        item_key="o:x",
        title_en="o:x",
        key_idea_ru=None,
        form=None,
        questions=[TaggedQuestion(_q("contrast"), None, "o:other")],  # a contrast question about another step
    )
    block = assemble_quiz(_bank(1, "t:a", "x"), [_pick(foreign, "due", same_topic=False)], rng=random.Random(3))
    assert all(q.item_key == "t:a" for q in block.questions)


def test_todays_contrast_question_rates_the_earlier_step_it_checks() -> None:
    content = TopicContent.model_validate(topic_content_payload())
    step = content.step(2)
    assert step.practice is not None
    quiz = [q.model_copy(update={"item_slug": "singular"}) if i == 0 else q for i, q in enumerate(step.practice.quiz)]
    step = step.model_copy(update={"practice": step.practice.model_copy(update={"quiz": quiz})})

    bank = bank_from_step(step, {"there_is_are:singular": 5, "there_is_are:plural": 6})

    assert bank.item_id == 6 and bank.item_key == "there_is_are:plural"
    assert (bank.questions[0].item_key, bank.questions[0].item_id) == ("there_is_are:singular", 5)
    assert len(bank.own_questions) == 4
    assert all(isinstance(tagged.question, QuizQuestion) for tagged in bank.questions)


def test_quiz_prefers_questions_not_asked_before() -> None:
    today = _bank(1, "t:a", "x")
    asked = AskedHistory(questions={"t:a": {f"Question x{i} here" for i in range(3)}})
    block = assemble_quiz(today, [], asked=asked, rng=random.Random(3))
    texts = [q.question.question for q in block.questions]
    assert set(texts[:2]) == {"Question x3 here", "Question x4 here"}


def test_translation_has_two_today_and_the_planned_one() -> None:
    block = assemble_translation(
        _bank(1, "t:a", "x"), _pick(_bank(3, "o:c", "z"), "wrong_today", same_topic=False), rng=random.Random(0)
    )
    assert [(s.source, s.reason) for s in block.sentences] == [
        ("today", "today"),
        ("today", "today"),
        ("review", "wrong_today"),
    ]


def test_translation_without_a_pick_fills_from_today_preferring_new_sentences() -> None:
    asked = AskedHistory(sentences={"t:a": {"Предложение про x0"}})
    block = assemble_translation(_bank(1, "t:a", "x"), None, asked=asked, rng=random.Random(0))
    assert len(block.sentences) == TRANSLATION_SIZE
    assert block.sentences[-1].task.ru == "Предложение про x0"  # the one asked before comes last


def test_bank_from_lesson_uses_the_core_item() -> None:
    lesson = Lesson.model_validate(lesson_payload())
    bank = bank_from_lesson(lesson, "there_is_are:core", item_id=4)
    assert bank.item_key == "there_is_are:core" and bank.item_id == 4
    assert len(bank.questions) == 3 and len(bank.sentences) == 3
    assert all(tagged.item_id == 4 for tagged in bank.questions)
