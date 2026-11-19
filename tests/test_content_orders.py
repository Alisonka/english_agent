from __future__ import annotations

import pytest
from pydantic import ValidationError

from english_agent.models.content_order import ContentBrief, brief_topic
from english_agent.models.curriculum import all_topics


def remedial(**changes: object) -> ContentBrief:
    values: dict[str, object] = {
        "kind": "remedial",
        "topic_ids": ["past_simple_irregular"],
        "error_type": "form",
        "focus_ru": "Третья форма неправильных глаголов.",
    }
    return ContentBrief.model_validate({**values, **changes})


def test_a_brief_is_shared_by_its_topic_and_gap() -> None:
    assert remedial().topic_id == "remedial_past_simple_irregular_form"
    assert remedial(focus_ru="Другая формулировка того же.").topic_id == remedial().topic_id  # shared
    a = ContentBrief(kind="contrast", topic_ids=["past_simple_regular", "past_continuous"], focus_ru="Когда что.")
    b = ContentBrief(kind="contrast", topic_ids=["past_continuous", "past_simple_regular"], focus_ru="Иначе.")
    assert a.topic_id == b.topic_id == "contrast_past_continuous_past_simple_regular"
    assert (remedial().steps, a.steps) == (1, 2)


def test_long_ids_stay_within_the_column_and_distinct() -> None:
    long_a = ContentBrief(
        kind="contrast",
        topic_ids=["present_perfect_simple_vs_continuous", "inversion_conditionals_advanced"],
        focus_ru="Разница.",
    )
    long_b = long_a.model_copy(update={"topic_ids": ["present_perfect_simple_vs_continuous", "formal_conditionals"]})
    assert len(long_a.topic_id) <= 90 and long_a.topic_id != long_b.topic_id


@pytest.mark.parametrize(
    "changes",
    [{"error_type": None}, {"topic_ids": ["a", "b"]}, {"kind": "contrast", "topic_ids": ["a", "a"]}],
)
def test_malformed_briefs_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        remedial(**changes)


def test_a_brief_becomes_a_curriculum_topic_with_its_focus() -> None:
    topic = brief_topic(remedial(), all_topics())
    assert topic.id == "remedial_past_simple_irregular_form" and "remedial" in topic.tags
    assert "Третья форма" in topic.title_ru and len(topic.title_ru) <= 80
