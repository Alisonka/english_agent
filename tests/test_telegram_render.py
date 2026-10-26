from __future__ import annotations

import re

import pytest

from conftest import lesson_payload
from english_agent.models.lesson import EXAMPLE_MAX, TELEGRAM_MAX_MESSAGE_LENGTH, Example, Lesson
from english_agent.telegram.render import highlight, render_blocks, render_lesson, split_message

_TAG = re.compile(r"<(/?)(b|i|s|code|a)\b[^>]*>")


def assert_balanced_tags(text: str) -> None:
    """Every opening tag is closed in order, and nothing stays open at the end."""
    stack: list[str] = []
    for closing, name in _TAG.findall(text):
        if closing:
            assert stack and stack.pop() == name, f"unbalanced </{name}> in: {text[:80]!r}"
        else:
            stack.append(name)
    assert not stack, f"unclosed tags {stack} in: {text[:80]!r}"


def test_render_contains_every_lesson_field(lesson: Lesson) -> None:
    text = render_lesson(lesson)

    assert f"<b>{lesson.title_en}</b>" in text
    assert lesson.title_ru in text
    assert f"<code>{lesson.form}</code>" in text
    assert lesson.rule_ru in text
    for nuance in lesson.nuances_ru:
        assert f"• {nuance}" in text
    for example in lesson.examples:
        assert f"<i>{example.ru}</i>" in text
    assert lesson.mistake_ru in text
    assert f"<s>{lesson.incorrect}</s>" in text
    assert lesson.correct in text
    assert f'<a href="{lesson.sources[0].url}">{lesson.sources[0].title}</a>' in text


def test_render_escapes_html_in_model_text() -> None:
    lesson = Lesson.model_validate(
        lesson_payload(
            title_en="a < b & c > d",
            form="<verb> & <noun>",
            examples=[
                {"en": "Tom & Jerry <3 cheese.", "ru": "Том и Джерри любят сыр.", "highlights": ["& Jerry"]},
                *lesson_payload()["examples"][:3],
            ],
        )
    )
    text = render_lesson(lesson)

    assert "<b>a &lt; b &amp; c &gt; d</b>" in text
    assert "<code>&lt;verb&gt; &amp; &lt;noun&gt;</code>" in text
    assert "Tom <b>&amp; Jerry</b> &lt;3 cheese." in text
    assert "<3" not in text
    assert_balanced_tags(text)


def test_highlight_wraps_every_occurrence_and_merges_overlaps() -> None:
    example = Example(
        en="There is a cat and there is a dog. This is it.",
        ru="Кот и собака.",
        highlights=["There is", "there is a", "is"],
    )
    text = highlight(example)

    # Overlapping spans ("there is a" + "is") collapse into one <b>, never nested tags.
    assert text == "<b>There is</b> a cat and <b>there is a</b> dog. Th<b>is</b> <b>is</b> it."
    assert "<b><b>" not in text
    assert_balanced_tags(text)


def test_highlight_without_match_in_sentence_is_escaped_plain_text() -> None:
    example = Example.model_construct(en="Fish & chips.", ru="Рыба с картошкой.", highlights=["pizza"])
    assert highlight(example) == "Fish &amp; chips."


def test_every_rendered_line_is_tag_balanced(lesson: Lesson) -> None:
    for line in render_lesson(lesson).split("\n"):
        assert_balanced_tags(line)


def test_render_blocks_are_in_reading_order(lesson: Lesson) -> None:
    headings = [block.split("\n", 1)[0] for block in render_blocks(lesson)]
    assert headings[0] == f"<b>{lesson.title_en}</b>"
    assert headings[1:] == [
        "<b>Формула</b>",
        "<b>Правило</b>",
        "<b>Нюансы</b>",
        "<b>Примеры</b>",
        "<b>Типичная ошибка</b>",
        "<b>Источники</b>",
    ]


def test_split_message_keeps_short_text_whole() -> None:
    assert split_message("hello", limit=10) == ["hello"]
    assert split_message("", limit=10) == [""]


def test_split_message_prefers_paragraph_boundaries() -> None:
    text = "para one\n\npara two\n\npara three"
    assert split_message(text, limit=20) == ["para one\n\npara two", "para three"]


def test_split_message_falls_back_to_line_boundaries() -> None:
    text = "line one\nline two\nline three\n\nnext"
    chunks = split_message(text, limit=18)
    # The overlong first paragraph breaks on lines; its tail still packs with the next paragraph.
    assert chunks == ["line one\nline two", "line three\n\nnext"]
    assert all(len(chunk) <= 18 for chunk in chunks)


def test_split_message_hard_cuts_a_single_overlong_line() -> None:
    assert split_message("abcdefghij", limit=4) == ["abcd", "efgh", "ij"]


def test_split_message_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        split_message("x", limit=0)


def test_split_message_never_exceeds_limit_and_loses_no_text() -> None:
    text = "\n\n".join("\n".join(f"p{p} l{line} " + "x" * (line * 7 % 50) for line in range(6)) for p in range(12))
    for limit in (30, 64, 100, 257, 1000):
        chunks = split_message(text, limit=limit)
        assert all(0 < len(chunk) <= limit for chunk in chunks)
        assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_maximal_lesson_splits_into_valid_telegram_messages() -> None:
    """A lesson filling every field bound still goes out as tag-balanced chunks."""
    sentence = "There is a very long sentence here. "
    examples = [
        {
            "en": f"There is example {i} " + "x" * (240 - len(f"There is example {i} ")),
            "ru": "й" * 240,
            "highlights": ["There is"],
        }
        for i in range(EXAMPLE_MAX)
    ]
    lesson = Lesson.model_validate(
        lesson_payload(
            rule_ru=(sentence * 60)[:2000],
            nuances_ru=[("н" * 20 + f" {i} " + "ю" * 380)[:400] for i in range(8)],
            examples=examples,
            mistake_ru="м" * 400,
            incorrect="x" * 240,
            correct="y" * 240,
            sources=[lesson_payload()["sources"][0]] * 5,
        )
    )
    text = render_lesson(lesson)
    assert len(text) > TELEGRAM_MAX_MESSAGE_LENGTH

    chunks = split_message(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert_balanced_tags(chunk)
