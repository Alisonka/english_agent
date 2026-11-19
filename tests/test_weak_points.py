from __future__ import annotations

from datetime import UTC, datetime

from conftest import topic_content_payload
from english_agent.models.profile import FocusPoint
from english_agent.models.storage import ErrorRecord, ItemAccuracy
from english_agent.models.topic import TopicContent
from english_agent.profile import weak_points_for_topic
from english_agent.telegram.render import render_cheat_sheet

CONTENT = TopicContent.model_validate(topic_content_payload())
STEPS = CONTENT.steps


def _error(item_key: str) -> ErrorRecord:
    return ErrorRecord(
        id=1,
        learner_id=1,
        item_id=1,
        kind="quiz",
        original="It is a cat",
        corrected="There is a cat",
        error_type="word_order",
        explanation_ru="x",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


def test_weak_points_lists_struggled_steps_weakest_first() -> None:
    accuracy = {
        "there_is_are:singular": ItemAccuracy(item_id=1, checks=4, correct=1, days=2),  # 25%
        "there_is_are:plural": ItemAccuracy(item_id=2, checks=4, correct=3, days=2),  # 75%
    }
    points = weak_points_for_topic(STEPS, accuracy, {})
    assert [point.title_ru for point in points] == [STEPS[0].title_ru, STEPS[1].title_ru]
    assert points[0].accuracy == 0.25


def test_weak_points_skips_fully_correct_steps_without_errors() -> None:
    accuracy = {"there_is_are:singular": ItemAccuracy(item_id=1, checks=3, correct=3, days=2)}
    assert weak_points_for_topic(STEPS, accuracy, {}) == []


def test_weak_points_include_a_step_with_an_error_even_without_accuracy() -> None:
    errors = {"there_is_are:plural": [_error("there_is_are:plural")]}
    points = weak_points_for_topic(STEPS, {}, errors)
    assert len(points) == 1 and points[0].title_ru == STEPS[1].title_ru
    assert points[0].example == "It is a cat -> There is a cat"


def test_a_reflector_focus_point_adds_its_hint_to_the_step() -> None:
    accuracy = {"there_is_are:plural": ItemAccuracy(item_id=2, checks=4, correct=2, days=2)}
    focus = [
        FocusPoint(
            item_keys=["there_is_are:plural"],
            pattern_ru="Ставит there is перед множественным числом.",
            contrast_ru="Много предметов — there are.",
            error_type="target_form",
            evidence_error_ids=[1],
        )
    ]
    [point] = weak_points_for_topic(STEPS, accuracy, {}, focus)
    assert point.contrast_ru == "Много предметов — there are."
    assert "💡 <i>Много предметов — there are.</i>" in render_cheat_sheet(CONTENT.summary, [point])[0]
