from __future__ import annotations

from conftest import topic_content_payload
from english_agent.models.lesson import TELEGRAM_MAX_MESSAGE_LENGTH
from english_agent.models.profile import TopicWeakPoint
from english_agent.models.topic import STEP_MESSAGE_LIMIT, TopicContent
from english_agent.telegram.render import count_lines, progress_bar, render_cheat_sheet, render_step

CONTENT = TopicContent.model_validate(topic_content_payload())


def test_render_step_is_short_and_scannable() -> None:
    message = render_step(CONTENT.step(1))
    assert len(message) <= STEP_MESSAGE_LIMIT
    assert count_lines(message) <= 12
    assert "<tg-spoiler>" in message and "📘" in message and "шаг 1 из 2" in message


def test_render_step_alt_marks_the_reteach() -> None:
    assert "ещё раз, по-другому" in render_step(CONTENT.step(1, "alt"))


def test_render_step_last_step_has_no_teaser() -> None:
    assert "Завтра" not in render_step(CONTENT.step(2))


def test_progress_bar() -> None:
    assert progress_bar(1, 3) == "▰▱▱"
    assert progress_bar(3, 3) == "▰▰▰"


def test_cheat_sheet_without_weak_points() -> None:
    messages = render_cheat_sheet(CONTENT.summary, [])
    body = "\n".join(messages)
    assert "Шпаргалка" in body and "Ошибок по теме не было" in body
    assert all(len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH for message in messages)


def test_cheat_sheet_with_weak_points() -> None:
    weak = [TopicWeakPoint(title_ru="there is", key_idea_ru="один предмет", accuracy=0.4)]
    body = "\n".join(render_cheat_sheet(CONTENT.summary, weak))
    assert "слабые места" in body and "40%" in body
