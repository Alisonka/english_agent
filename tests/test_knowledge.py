from __future__ import annotations

from datetime import UTC, datetime

import pytest

from english_agent.knowledge import (
    LEVEL_DIFFICULTY,
    EloDelta,
    elo_step,
    probability,
    question_key,
    start_ability,
    start_difficulty,
    topic_meta,
)
from english_agent.models.lesson import Lesson
from english_agent.storage import EvidenceRepo, KnowledgeRepo, LearnerRepo, SessionFactory
from test_storage import _quiz_setup, wrong_option

GRID = [(ability, easy, gap) for ability in (-2.0, 0.0, 2.5) for easy in (-2.0, 0.5, 2.0) for gap in (0.1, 1.0, 3.0)]


def test_question_keys_are_stable_and_distinct() -> None:
    assert question_key("t:core", "Where is he?") == question_key("t:core", "Where is he?")
    assert question_key("t:core", "Where is he?") != question_key("t:core", "Where was he?")
    assert question_key("t:core", "x").startswith("t:core:")


@pytest.mark.parametrize(("ability", "easy", "gap"), GRID)
def test_a_right_answer_to_a_harder_question_raises_ability_more(ability: float, easy: float, gap: float) -> None:
    on_easy = elo_step(ability, {}, easy, 0, correct=True)
    on_hard = elo_step(ability, {}, easy + gap, 0, correct=True)
    assert on_hard.learner > on_easy.learner > 0
    # ...and a wrong answer to an easier one lowers it more.
    assert elo_step(ability, {}, easy, 0, correct=False).learner < elo_step(ability, {}, easy + gap, 0, False).learner
    assert on_hard.question < 0  # answered right: the question looks easier than thought


def test_steps_follow_the_prediction_and_calibration_settles() -> None:
    fresh = elo_step(0.0, {"tenses": 0.0}, 0.0, 0, correct=True)
    assert fresh.p == pytest.approx(0.5)
    assert fresh.tags == {"tenses": pytest.approx(fresh.learner / 2)}
    settled = elo_step(0.0, {}, 0.0, 200, correct=True)
    assert abs(settled.question) < abs(fresh.question) / 5
    assert probability(1.0, [0.5, -0.5], 1.0) == pytest.approx(0.5)  # tag offsets are averaged


def test_start_estimates_follow_the_level_and_the_kind() -> None:
    meta = topic_meta("there_is_are")
    assert meta is not None and meta.cefr == "A2" and meta.tags
    assert start_difficulty(meta, "translation") > start_difficulty(meta, "quiz") == LEVEL_DIFFICULTY["A2"]
    assert start_difficulty(None, "quiz") == 0.0 and topic_meta("nope") is None
    # A learner at their own level answers its quiz questions right more often than not.
    assert probability(start_ability("A2"), [], start_difficulty(meta, "quiz")) > 0.5


async def test_graded_answers_move_ability_and_difficulty_and_keep_the_delta(
    sessions: SessionFactory, lesson: Lesson
) -> None:
    practice, _, item_id = await _quiz_setup(sessions, lesson)
    learner = await LearnerRepo(sessions).get_or_create("42", "A2")
    knowledge = KnowledgeRepo(sessions)
    assert await knowledge.ability(learner.id) is None

    await practice.record_poll_answer("p0", "42", [lesson.quiz[0].correct_option])
    await practice.record_poll_answer("p1", "42", wrong_option(lesson, 1))

    evidence = await EvidenceRepo(sessions).since(learner.id, datetime.min.replace(tzinfo=UTC))
    deltas = [EloDelta.model_validate(e.payload["elo"]) for e in evidence]
    assert deltas[0].learner > 0 > deltas[1].learner
    ability = await knowledge.ability(learner.id)
    assert ability is not None and ability.answers == 2
    # The stored moves add up to the current ratings: reversing them is exact.
    assert ability.overall - sum(d.learner for d in deltas) == pytest.approx(start_ability("A2"))
    for tag, rating in ability.tags.items():
        assert rating == pytest.approx(sum(d.tags[tag] for d in deltas))

    keys = [e.payload["question_key"] for e in evidence]
    stats = await knowledge.questions(keys)
    assert {stat.answers for stat in stats.values()} == {1} and {s.item_id for s in stats.values()} == {item_id}
    assert [s.question_key for s in await knowledge.item_questions([item_id])] == sorted(keys)
