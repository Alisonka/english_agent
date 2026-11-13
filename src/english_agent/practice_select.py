"""Choosing the day's practice from several step banks: pure, no I/O, so the rules are testable.

A day's quiz interleaves today's step with an earlier step of the same topic and an older topic;
the translation does the same with fewer items. The caller (practice delivery) gathers the banks
and the learner's cards, these functions decide what to ask, and the result is stored as the
delivery's `PracticeBlock`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from english_agent.models.lesson import Lesson
from english_agent.models.practice import DAILY_QUIZ_SIZE, TRANSLATION_SIZE, QuizQuestion, TranslationTask
from english_agent.models.storage import AssembledQuestion, AssembledSentence, PracticeBlock, PracticeSource
from english_agent.models.topic import LessonStep


@dataclass(frozen=True)
class StepBank:
    """The questions and sentences available for one item (a step, or a legacy lesson's core item)."""

    item_id: int | None
    item_key: str
    title_en: str
    key_idea_ru: str | None
    form: str | None
    questions: list[QuizQuestion] = field(default_factory=list)
    sentences: list[TranslationTask] = field(default_factory=list)


def bank_from_step(step: LessonStep, item_id: int | None) -> StepBank:
    practice = step.practice
    return StepBank(
        item_id=item_id,
        item_key=step.item_key,
        title_en=step.title_en,
        key_idea_ru=step.message.key_idea_ru,
        form=step.message.form,
        questions=[QuizQuestion(**q.model_dump(exclude={"item_slug"})) for q in practice.quiz] if practice else [],
        sentences=(
            [TranslationTask(**s.model_dump(exclude={"item_slug"})) for s in practice.translation] if practice else []
        ),
    )


def bank_from_lesson(lesson: Lesson, item_key: str, item_id: int | None) -> StepBank:
    return StepBank(
        item_id=item_id,
        item_key=item_key,
        title_en=lesson.title_en,
        key_idea_ru=lesson.key_idea_ru,
        form=lesson.form,
        questions=list(lesson.quiz),
        sentences=list(lesson.translation),
    )


def choose_previous(banks: list[StepBank], retrievability: dict[int, float]) -> StepBank | None:
    """Of the earlier steps, the one least likely to be recalled (an unseen item counts as 0)."""
    if not banks:
        return None
    return min(banks, key=lambda bank: retrievability.get(bank.item_id, 0.0) if bank.item_id is not None else 0.0)


def _pick_questions(
    bank: StepBank,
    count: int,
    source: PracticeSource,
    *,
    asked: dict[str, set[str]],
    used: set[str],
    rng: random.Random,
) -> list[AssembledQuestion]:
    pool = [question for question in bank.questions if question.question not in used]
    seen = asked.get(bank.item_key, set())
    unasked = [question for question in pool if question.question not in seen]
    repeats = [question for question in pool if question.question in seen]
    rng.shuffle(unasked)
    rng.shuffle(repeats)
    chosen = (unasked + repeats)[:count]
    for question in chosen:
        used.add(question.question)
    return [
        AssembledQuestion(question=question, item_id=bank.item_id, item_key=bank.item_key, source=source)
        for question in chosen
    ]


def assemble_quiz(
    today: StepBank,
    previous: StepBank | None,
    review: StepBank | None,
    *,
    asked: dict[str, set[str]] | None = None,
    rng: random.Random | None = None,
) -> PracticeBlock:
    """The day's quiz: 2 from today's step, 1 from an earlier step, 1 from an older topic.

    When there is no earlier step or no older-topic card, the slot is filled from the next best
    bank, so the quiz always has `DAILY_QUIZ_SIZE` questions as long as today's bank has enough.
    """
    asked = asked or {}
    rng = rng or random.Random()
    used: set[str] = set()
    picks = _pick_questions(today, 2, "today", asked=asked, used=used, rng=rng)
    if previous is not None:
        picks += _pick_questions(previous, 1, "previous", asked=asked, used=used, rng=rng)
    if review is not None:
        picks += _pick_questions(review, 1, "review", asked=asked, used=used, rng=rng)
    fillers = [(today, "today"), (previous, "previous"), (review, "review")]
    while len(picks) < DAILY_QUIZ_SIZE:
        added = False
        for bank, source in fillers:
            if bank is None or len(picks) >= DAILY_QUIZ_SIZE:
                continue
            more = _pick_questions(bank, 1, source, asked=asked, used=used, rng=rng)  # type: ignore[arg-type]
            if more:
                picks += more
                added = True
        if not added:
            break
    return PracticeBlock(kind="quiz", questions=picks[:DAILY_QUIZ_SIZE])


def _pick_sentences(
    bank: StepBank, count: int, source: PracticeSource, *, used: set[str], rng: random.Random
) -> list[AssembledSentence]:
    pool = [sentence for sentence in bank.sentences if sentence.ru not in used]
    rng.shuffle(pool)
    chosen = pool[:count]
    for sentence in chosen:
        used.add(sentence.ru)
    return [
        AssembledSentence(
            task=sentence,
            item_id=bank.item_id,
            item_key=bank.item_key,
            title_en=bank.title_en,
            key_idea_ru=bank.key_idea_ru,
            form=bank.form,
            source=source,
        )
        for sentence in chosen
    ]


def assemble_translation(
    today: StepBank,
    other: StepBank | None,
    *,
    other_source: PracticeSource = "previous",
    rng: random.Random | None = None,
) -> PracticeBlock:
    """The day's translation: 2 sentences from today's step, 1 from an earlier step or older topic."""
    rng = rng or random.Random()
    used: set[str] = set()
    picks = _pick_sentences(today, 2, "today", used=used, rng=rng)
    if other is not None:
        picks += _pick_sentences(other, 1, other_source, used=used, rng=rng)
    fillers = [(today, "today"), (other, other_source)]
    while len(picks) < TRANSLATION_SIZE:
        added = False
        for bank, source in fillers:
            if bank is None or len(picks) >= TRANSLATION_SIZE:
                continue
            more = _pick_sentences(bank, 1, source, used=used, rng=rng)  # type: ignore[arg-type]
            if more:
                picks += more
                added = True
        if not added:
            break
    return PracticeBlock(kind="translation", sentences=picks[:TRANSLATION_SIZE])
