"""Validation and helpers of the step-based topic models (models/topic.py)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import topic_content_payload
from english_agent.models.topic import (
    STEP_TEXT_BUDGET,
    LessonStep,
    PlanStep,
    StepExample,
    StepMessage,
    TopicContent,
)
from samples import STEP_MESSAGE_SINGULAR


def _main_step_payload() -> dict[str, Any]:
    return copy.deepcopy(topic_content_payload()["steps"][0])


def test_topic_content_fixture_validates() -> None:
    content = TopicContent.model_validate(topic_content_payload())
    assert content.item_keys() == ["there_is_are:singular", "there_is_are:plural"]
    assert content.step(1).slug == "singular"
    assert content.step(2, "alt").variant == "alt"


def test_form_for_takes_the_nearest_form_at_or_before_index() -> None:
    payload = topic_content_payload()
    # Second step introduces no new form: the first step's form stays in force.
    payload["steps"][1]["message"] = {**STEP_MESSAGE_SINGULAR, "form": None, "teaser_ru": None}
    payload["alternatives"][1]["message"] = {**STEP_MESSAGE_SINGULAR, "form": None, "teaser_ru": None}
    content = TopicContent.model_validate(payload)
    assert content.form_for(1) == STEP_MESSAGE_SINGULAR["form"]
    assert content.form_for(2) == STEP_MESSAGE_SINGULAR["form"]


def test_step_example_rejects_a_long_sentence() -> None:
    with pytest.raises(ValidationError):
        StepExample(en="x" * 91, ru="кот", highlights=["x"])


def test_step_message_over_budget_is_rejected() -> None:
    payload = copy.deepcopy(STEP_MESSAGE_SINGULAR)
    payload["hook_ru"] = "а" * 140  # max every long field so the visible-text sum clears the budget
    payload["key_idea_ru"] = "и" * 160
    payload["contrast_ru"] = "о" * 160
    payload["incorrect"] = "e" * 90
    payload["correct"] = "c" * 90
    payload["teaser_ru"] = "т" * 100
    with pytest.raises(ValidationError) as error:
        StepMessage.model_validate(payload)
    assert "budget" in str(error.value) and STEP_TEXT_BUDGET == 950


def test_teaser_required_on_every_step_but_the_last() -> None:
    payload = _main_step_payload()
    payload["message"] = {**STEP_MESSAGE_SINGULAR, "teaser_ru": None}  # step 1 of 2 without a teaser
    with pytest.raises(ValidationError):
        LessonStep.model_validate(payload)


def test_teaser_forbidden_on_the_last_step() -> None:
    payload = _main_step_payload()
    payload["index"] = 2  # last of 2, but the singular message carries a teaser
    with pytest.raises(ValidationError):
        LessonStep.model_validate(payload)


def test_main_step_requires_practice() -> None:
    payload = _main_step_payload()
    payload["practice"] = None
    with pytest.raises(ValidationError):
        LessonStep.model_validate(payload)


def test_alt_step_rejects_practice() -> None:
    payload = _main_step_payload()
    payload["variant"] = "alt"  # keeps the practice bank, which an alt step must not carry
    with pytest.raises(ValidationError):
        LessonStep.model_validate(payload)


@pytest.mark.parametrize("reserved", ["core", "summary"])
def test_reserved_slugs_are_rejected(reserved: str) -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            slug=reserved,
            title_en="x",
            title_ru="х",
            key_idea_ru="Длинная ключевая мысль не короче тридцати символов точно.",
        )


def test_plan_rejects_duplicate_slugs() -> None:
    payload = topic_content_payload()
    payload["plan"]["steps"][1]["slug"] = "singular"
    with pytest.raises(ValidationError):
        TopicContent.model_validate(payload)


def test_topic_content_rejects_misaligned_lengths() -> None:
    payload = topic_content_payload()
    payload["alternatives"] = payload["alternatives"][:1]  # one alt against two main steps
    with pytest.raises(ValidationError):
        TopicContent.model_validate(payload)


def test_topic_content_rejects_slug_mismatch_with_plan() -> None:
    payload = topic_content_payload()
    payload["steps"][0]["slug"] = "other"  # no longer matches the plan's first slug
    with pytest.raises(ValidationError):
        TopicContent.model_validate(payload)
