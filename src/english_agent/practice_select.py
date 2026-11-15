"""Assembling the day's practice from step banks, as the Planner chose: pure, no I/O.

`planner.plan_quiz` / `plan_translation` decide which items the day reviews; the caller resolves
each to its step's bank and these functions pick the questions and sentences: unasked ones first,
never the same twice in a block. A step's contrast question can check an earlier step: each
question keeps the item it checks, and a review of an item only asks that item's own questions.
The result is stored as the delivery's `PracticeBlock`.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field

from english_agent.models.lesson import Lesson
from english_agent.models.practice import DAILY_QUIZ_SIZE, TRANSLATION_SIZE, QuizQuestion, TranslationTask
from english_agent.models.storage import (
    AskedHistory,
    AssembledQuestion,
    AssembledSentence,
    LessonRecord,
    PlanReason,
    PracticeBlock,
    PracticeSource,
)
from english_agent.models.topic import LessonStep
from english_agent.planner import Pick

TODAY_QUESTIONS = 2
TODAY_SENTENCES = 2


@dataclass(frozen=True)
class TaggedQuestion:
    """A bank question and the item it checks (its step's, or an earlier step's for a contrast)."""

    question: QuizQuestion
    item_id: int | None
    item_key: str


@dataclass(frozen=True)
class StepBank:
    """The questions and sentences available for one item (a step, or a legacy lesson's core item)."""

    item_id: int | None
    item_key: str
    title_en: str
    key_idea_ru: str | None
    form: str | None
    questions: list[TaggedQuestion] = field(default_factory=list)
    sentences: list[TranslationTask] = field(default_factory=list)

    @property
    def own_questions(self) -> list[TaggedQuestion]:
        return [question for question in self.questions if question.item_key == self.item_key]


def bank_from_step(step: LessonStep, item_ids: Mapping[str, int]) -> StepBank:
    """A step's bank; `item_ids` maps item keys to ids (this step's and, for contrasts, earlier steps')."""
    practice = step.practice
    questions: list[TaggedQuestion] = []
    sentences: list[TranslationTask] = []
    if practice is not None:
        for question in practice.quiz:
            key = f"{step.topic_id}:{question.item_slug}"
            plain = QuizQuestion(**question.model_dump(exclude={"item_slug"}))
            questions.append(TaggedQuestion(question=plain, item_id=item_ids.get(key), item_key=key))
        sentences = [TranslationTask(**sentence.model_dump(exclude={"item_slug"})) for sentence in practice.translation]
    return StepBank(
        item_id=item_ids.get(step.item_key),
        item_key=step.item_key,
        title_en=step.title_en,
        key_idea_ru=step.message.key_idea_ru,
        form=step.message.form,
        questions=questions,
        sentences=sentences,
    )


def bank_from_lesson(lesson: Lesson, item_key: str, item_id: int | None) -> StepBank:
    return StepBank(
        item_id=item_id,
        item_key=item_key,
        title_en=lesson.title_en,
        key_idea_ru=lesson.key_idea_ru,
        form=lesson.form,
        questions=[TaggedQuestion(question=question, item_id=item_id, item_key=item_key) for question in lesson.quiz],
        sentences=list(lesson.translation),
    )


def bank_from_record(record: LessonRecord, item_id: int | None) -> StepBank | None:
    """The practice bank a sent day offers for its item: its step's bank, or a legacy lesson's core bank."""
    step = record.step
    if step is not None:
        if step.practice is None:
            return None
        return bank_from_step(step, {step.item_key: item_id} if item_id is not None else {})
    lesson = record.lesson
    if lesson is not None:
        return bank_from_lesson(lesson, record.item_key, item_id)
    return None


def _pick_questions(
    bank: StepBank,
    count: int,
    source: PracticeSource,
    reason: PlanReason,
    *,
    own_only: bool,
    asked: Mapping[str, set[str]],
    used: set[str],
    rng: random.Random,
) -> list[AssembledQuestion]:
    pool = [q for q in (bank.own_questions if own_only else bank.questions) if q.question.question not in used]
    unasked = [q for q in pool if q.question.question not in asked.get(q.item_key, set())]
    repeats = [q for q in pool if q.question.question in asked.get(q.item_key, set())]
    rng.shuffle(unasked)
    rng.shuffle(repeats)
    chosen = (unasked + repeats)[:count]
    for tagged in chosen:
        used.add(tagged.question.question)
    return [
        AssembledQuestion(
            question=tagged.question, item_id=tagged.item_id, item_key=tagged.item_key, source=source, reason=reason
        )
        for tagged in chosen
    ]


def assemble_quiz(
    today: StepBank,
    picks: list[tuple[Pick, StepBank]],
    *,
    asked: AskedHistory | None = None,
    rng: random.Random | None = None,
) -> PracticeBlock:
    """Two questions on today's step and one per planned pick, at least `DAILY_QUIZ_SIZE` in all.

    A short plan (a first topic without reviews) is filled from today's bank, then from the picks'.
    """
    history = asked.questions if asked is not None else {}
    rng = rng or random.Random()
    used: set[str] = set()
    sources: list[tuple[StepBank, PracticeSource, PlanReason, bool]] = [(today, "today", "today", False)]
    sources += [(bank, pick.source, pick.reason, True) for pick, bank in picks]

    def take(index: int, count: int) -> list[AssembledQuestion]:
        bank, source, reason, own_only = sources[index]
        return _pick_questions(bank, count, source, reason, own_only=own_only, asked=history, used=used, rng=rng)

    chosen = take(0, TODAY_QUESTIONS)
    for index in range(1, len(sources)):
        chosen += take(index, 1)
    target = max(DAILY_QUIZ_SIZE, len(chosen))
    while len(chosen) < target:
        before = len(chosen)
        for index in range(len(sources)):
            if len(chosen) < target:
                chosen += take(index, 1)
        if len(chosen) == before:
            break
    return PracticeBlock(kind="quiz", questions=chosen)


def _pick_sentences(
    bank: StepBank,
    count: int,
    source: PracticeSource,
    reason: PlanReason,
    *,
    asked: Mapping[str, set[str]],
    used: set[str],
    rng: random.Random,
) -> list[AssembledSentence]:
    seen = asked.get(bank.item_key, set())
    pool = [sentence for sentence in bank.sentences if sentence.ru not in used]
    unasked = [sentence for sentence in pool if sentence.ru not in seen]
    repeats = [sentence for sentence in pool if sentence.ru in seen]
    rng.shuffle(unasked)
    rng.shuffle(repeats)
    chosen = (unasked + repeats)[:count]
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
            reason=reason,
        )
        for sentence in chosen
    ]


def assemble_translation(
    today: StepBank,
    other: tuple[Pick, StepBank] | None,
    *,
    asked: AskedHistory | None = None,
    rng: random.Random | None = None,
) -> PracticeBlock:
    """Two sentences on today's step and one on the Planner's pick; filled from today's bank without one."""
    history = asked.sentences if asked is not None else {}
    rng = rng or random.Random()
    used: set[str] = set()
    sources: list[tuple[StepBank, PracticeSource, PlanReason]] = [(today, "today", "today")]
    if other is not None:
        pick, bank = other
        sources.append((bank, pick.source, pick.reason))

    def take(index: int, count: int) -> list[AssembledSentence]:
        bank, source, reason = sources[index]
        return _pick_sentences(bank, count, source, reason, asked=history, used=used, rng=rng)

    chosen = take(0, TODAY_SENTENCES)
    if len(sources) > 1:
        chosen += take(1, 1)
    while len(chosen) < TRANSLATION_SIZE:
        before = len(chosen)
        for index in range(len(sources)):
            if len(chosen) < TRANSLATION_SIZE:
                chosen += take(index, 1)
        if len(chosen) == before:
            break
    return PracticeBlock(kind="translation", sentences=chosen)
