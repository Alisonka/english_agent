from __future__ import annotations

from conftest import topic_content_payload
from english_agent.items import step_item_from
from english_agent.models.topic import TopicContent

CONTENT = TopicContent.model_validate(topic_content_payload())


def test_step_item_from_builds_a_step_keyed_item() -> None:
    draft = step_item_from(CONTENT.step(1), form="There is + noun")
    assert draft.key == "there_is_are:singular"
    assert draft.topic_id == "there_is_are" and draft.form == "There is + noun"
    assert draft.function_ru and draft.sources


def test_step_item_falls_back_to_the_steps_own_form() -> None:
    draft = step_item_from(CONTENT.step(1), form=None)
    assert draft.form == CONTENT.step(1).message.form
