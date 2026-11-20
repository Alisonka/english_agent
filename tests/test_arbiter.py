from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from english_agent.arbiter import arbitrate, overturned_sentences
from english_agent.knowledge import start_ability
from english_agent.models.lesson import Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import AssembledSentence, GradingInput, PracticeBlock
from english_agent.srs import GOOD
from english_agent.storage import (
    CardRepo,
    EvidenceRepo,
    KnowledgeRepo,
    LearnerRepo,
    SessionFactory,
    session_scope,
    sql_stores,
)
from english_agent.storage.orm import ErrorRow, ReviewLogRow
from telegram_fake import FakeTelegram, fake_sender
from test_storage import SCHEDULER, _quiz_setup


def grade(*verdicts: str) -> TranslationGrade:
    sentences = []
    for n, verdict in enumerate(verdicts, start=1):
        fixed = verdict == "fixed"
        sentences.append(
            {
                "n": n,
                "learner_text": "" if verdict == "missing" else f"sentence {n}",
                "verdict": verdict,
                "corrected": f"better {n}" if fixed else ("ref" if verdict == "missing" else f"sentence {n}"),
                "explanation_ru": "Так правильнее." if fixed else "",
                "error_type": "article" if fixed else None,
            }
        )
    return TranslationGrade.model_validate({"sentences": sentences, "focus_ru": "Артикли."})


async def graded_answer(sessions: SessionFactory, lesson: Lesson) -> tuple[int, int, int]:
    """A graded translation (1 correct, 2 fixed, 3 missing) on a step-day block; (learner, answer, item)."""
    practice, lesson_id, item_id = await _quiz_setup(sessions, lesson)
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    block = PracticeBlock(
        kind="translation",
        sentences=[
            AssembledSentence(task=task, item_id=item_id, item_key="there_is_are:core", title_en="t", source="today")
            for task in lesson.translation[:3]
        ],
    )
    task = await practice.claim_delivery(lesson_id, "translation", 3, payload=block.model_dump(mode="json"))
    assert task is not None
    await practice.mark_sent(task.id, [20])
    answer = await practice.start_translation_answer(task.id, learner.id, "my answer")
    assert answer is not None
    await practice.finish_translation_answer(answer.id, grade("correct", "fixed", "missing"))
    assert await practice.dispute(answer.id)
    return learner.id, answer.id, item_id


def judge(second: TranslationGrade) -> Any:
    seen: list[GradingInput] = []

    async def arbiter(task: GradingInput, text: str) -> TranslationGrade:
        seen.append(task)
        return second

    arbiter.seen = seen  # type: ignore[attr-defined]
    return arbiter


def test_only_corrected_sentences_the_arbiter_accepts_are_overturned() -> None:
    assert overturned_sentences(grade("correct", "fixed", "fixed"), grade("correct", "correct", "fixed")) == [2]
    assert (
        overturned_sentences(grade("correct", "missing", "fixed"), grade("correct", "correct", "fixed")) == []
    )  # blank stays blank
    assert (
        overturned_sentences(grade("correct", "correct", "fixed"), grade("fixed", "correct", "fixed")) == []
    )  # never lowers


async def test_an_overturned_sentence_is_undone_everywhere(sessions: SessionFactory, lesson: Lesson) -> None:
    learner_id, answer_id, item_id = await graded_answer(sessions, lesson)
    stores = sql_stores(sessions, SCHEDULER)
    telegram = FakeTelegram()
    arbiter = judge(grade("correct", "correct", "missing"))

    assert await arbitrate(stores, fake_sender(telegram), arbiter, answer_id) == "overturned"

    assert arbiter.seen[0].learner is None  # independent: no notes about the learner
    assert "засчитаны" in telegram.texts[-1]
    evidence = await EvidenceRepo(sessions).since(learner_id, datetime.min.replace(tzinfo=UTC))
    by_ref = {e.source_ref: e for e in evidence}
    assert by_ref[f"translation_answer:{answer_id}:2"].cancelled_at is not None
    assert by_ref[f"translation_answer:{answer_id}:1"].cancelled_at is None
    async with session_scope(sessions) as session:
        [error] = (await session.scalars(select(ErrorRow).where(ErrorRow.kind == "text"))).all()
        assert error.cancelled_at is not None
    # The ability keeps only the moves of the answers that still count.
    kept = [e for e in evidence if e.cancelled_at is None and "elo" in e.payload]
    ability = await KnowledgeRepo(sessions).ability(learner_id)
    assert ability is not None
    assert ability.overall == pytest.approx(start_ability("A2") + sum(e.payload["elo"]["learner"] for e in kept))
    # The card is as if only the right sentence had been rated: one GOOD, no AGAIN.
    card = await CardRepo(sessions, SCHEDULER).get(learner_id, item_id)
    assert card is not None and card.reviews == 1
    async with session_scope(sessions) as session:
        first = await session.scalar(
            select(ReviewLogRow).where(ReviewLogRow.source_ref == f"translation_answer:{answer_id}:1")
        )
    assert first is not None and first.payload is not None
    alone, _ = SCHEDULER.review(first.payload["before"]["fsrs_state"], GOOD, first.reviewed_at)
    assert card.fsrs_state == alone


async def test_an_upheld_grade_gets_the_fuller_explanation_and_one_dispute_per_answer(
    sessions: SessionFactory, lesson: Lesson
) -> None:
    learner_id, answer_id, item_id = await graded_answer(sessions, lesson)
    stores = sql_stores(sessions, SCHEDULER)
    telegram = FakeTelegram()
    before = await CardRepo(sessions, SCHEDULER).get(learner_id, item_id)

    assert (
        await arbitrate(stores, fake_sender(telegram), judge(grade("correct", "fixed", "missing")), answer_id)
        == "upheld"
    )
    assert "согласна" in telegram.texts[-1] and "better 2" in telegram.texts[-1]
    assert await CardRepo(sessions, SCHEDULER).get(learner_id, item_id) == before
    assert (
        await arbitrate(stores, fake_sender(telegram), judge(grade("correct", "correct", "missing")), answer_id)
        == "none"
    )
