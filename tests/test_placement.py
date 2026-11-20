from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from conftest import topic_content_payload
from english_agent.examiner import continue_exam
from english_agent.knowledge import LEVEL_DIFFICULTY
from english_agent.models.topic import TopicContent
from english_agent.placement import MAX_QUESTIONS, MIN_QUESTIONS, UNAVAILABLE, place, start_placement
from english_agent.storage import Stores
from fakes import FakeExams, FakeKnowledge, fake_stores
from telegram_fake import FakeTelegram, fake_sender

DAY = date(2026, 11, 20)
TOPICS = {  # a few written topics of every level: the test's bank
    "A2": ["be_present", "there_is_are", "articles_a_an", "comparatives"],
    "B1": ["present_tenses_review", "used_to", "second_conditional"],
    "B2": ["mixed_conditionals", "cleft_sentences_it"],
}


async def written(stores: Stores) -> None:
    for cefr, topics in TOPICS.items():
        for topic_id in topics:
            _, record = await stores.contents.claim(
                topic_id,
                cefr,
                1,
                "k",
                owner="w",
                now=datetime.now(UTC),
                stale_after=timedelta(minutes=5),
                max_attempts=3,
            )
            await stores.contents.finish(
                record.id, TopicContent.model_validate(topic_content_payload(topic_id, cefr=cefr))
            )


@pytest.mark.parametrize(
    ("ability", "level", "credited"),
    [
        (-3.0, "A2", []),
        (LEVEL_DIFFICULTY["A2"] + 1.5, "B1", ["A2"]),
        (LEVEL_DIFFICULTY["B2"] + 1.5, "C1", ["A2", "B1", "B2"]),
    ],
)
def test_the_estimate_places_the_learner_and_credits_the_levels_below(
    ability: float, level: str, credited: list[str]
) -> None:
    assert place(ability) == (level, credited)


async def test_a_strong_learner_is_placed_high_and_lower_topics_are_credited() -> None:
    stores, learners, _ = fake_stores()
    learner = learners.started("42", DAY)
    await written(stores)
    telegram = FakeTelegram()
    sender = fake_sender(telegram)

    exam = await start_placement(stores, sender, learner, DAY, "k")
    assert exam is not None and exam.kind == "placement"
    exams = stores.exams
    assert isinstance(exams, FakeExams)
    await continue_exam(stores, sender, exam.id, None)
    while exams.exams[exam.id].state == "running":
        poll_id, (_, key, _) = next((pid, poll) for pid, poll in exams.polls.items() if not poll[2])
        right = next(q.question.correct_option for q in exam.pool if q.question_key == key)
        await exams.record_answer(poll_id, "42", [right])
        await continue_exam(stores, sender, exam.id, None)

    finished = exams.exams[exam.id]
    assert MIN_QUESTIONS <= len(finished.answers) <= MAX_QUESTIONS
    placed = await learners.get("42")
    assert placed is not None and placed.cefr != "A2"
    knowledge = stores.knowledge
    assert isinstance(knowledge, FakeKnowledge)
    assert knowledge.marks[learner.id]["be_present"] == "credited"
    assert "Твой уровень" in telegram.texts[-1]


async def test_too_few_written_questions_means_choose_by_buttons() -> None:
    stores, learners, _ = fake_stores()
    learner = learners.started("42", DAY)
    telegram = FakeTelegram()
    assert await start_placement(stores, fake_sender(telegram), learner, DAY, "k") is None
    assert telegram.texts == [UNAVAILABLE]
