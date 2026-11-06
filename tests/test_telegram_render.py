from __future__ import annotations

import re

import pytest

from conftest import lesson_payload
from english_agent.models.lesson import EXAMPLE_MAX, TELEGRAM_MAX_MESSAGE_LENGTH, Example, Lesson
from english_agent.models.practice import TranslationGrade
from english_agent.telegram.render import (
    highlight,
    render_blocks,
    render_grade,
    render_lesson,
    render_messages,
    render_quiz_summary,
    split_message,
)

_TAG = re.compile(r"<(/?)(b|i|s|code|a|blockquote)\b[^>]*>")


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
    assert lesson.key_idea_ru in text
    assert f"<blockquote expandable>{lesson.rule_ru}</blockquote>" in text
    for nuance in lesson.nuances_ru:
        assert f"🔸 {nuance}" in text
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


def test_every_rendered_block_is_tag_balanced(lesson: Lesson) -> None:
    # The rule block spans several lines inside one <blockquote>, so balance is a
    # per-block property now, not a per-line one.
    for block in render_blocks(lesson):
        assert_balanced_tags(block)


def test_render_blocks_are_in_reading_order(lesson: Lesson) -> None:
    first_lines = [block.split("\n", 1)[0] for block in render_blocks(lesson)]
    assert first_lines[0] == f"🇬🇧 <b>{lesson.title_en}</b>"
    assert first_lines[1] == f"📐 <code>{lesson.form}</code>"
    assert first_lines[2].startswith("💡 <b>Суть:</b> ")
    assert first_lines[3:] == [
        "📖 <b>Как это работает</b>",
        "📝 <b>Примеры</b>",
        "⚡️ <b>Запомни</b>",
        "🚫 <b>Ошибка</b>",
        "🔗 <b>Источники</b>",
    ]


def test_rule_body_bolds_leading_labels_and_keeps_paragraph_air() -> None:
    rule_ru = (
        "Формы. am используется только с I, is с he, she, it и единственным числом, "
        "are с we, you, they и множественным числом.\n\n"
        "Отрицание. not ставится сразу после глагола to be: I am not late, it is not cold, we are not ready."
    )
    lesson = Lesson.model_validate(lesson_payload(rule_ru=rule_ru))
    rule_block = next(block for block in render_blocks(lesson) if block.startswith("📖"))

    assert "<b>Формы.</b> am используется" in rule_block
    assert "<b>Отрицание.</b> not ставится" in rule_block
    # The blank line between paragraphs survives inside the quote (breathing room).
    assert "множественным числом.\n\n<b>Отрицание.</b>" in rule_block
    assert_balanced_tags(rule_block)


def test_rule_body_leaves_a_normal_sentence_without_a_bold_label() -> None:
    # A long first clause (no short "Label." prefix) must not be mistaken for a label.
    rule_ru = (
        "В настоящем времени глагол to be связывает подлежащее с его описанием и почти всегда обязателен, "
        "в отличие от русского языка, где связка обычно опускается, и поэтому её так легко потерять в "
        "английском предложении, особенно когда после подлежащего идёт прилагательное или существительное."
    )
    lesson = Lesson.model_validate(lesson_payload(rule_ru=rule_ru))
    rule_block = next(block for block in render_blocks(lesson) if block.startswith("📖"))

    assert "<b>" not in rule_block.split("<blockquote expandable>", 1)[1]


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
            rule_ru=(sentence * 60)[:1200],
            nuances_ru=[("н" * 20 + f" {i} " + "ю" * 180)[:200] for i in range(4)],
            examples=examples,
            mistake_ru="м" * 400,
            incorrect="x" * 240,
            correct="y" * 240,
            sources=[lesson_payload()["sources"][0]] * 5,
        )
    )
    assert len(render_lesson(lesson)) > TELEGRAM_MAX_MESSAGE_LENGTH

    chunks = render_messages(lesson)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert_balanced_tags(chunk)
        # The expandable rule quote is never cut across messages.
        assert chunk.count("<blockquote") == chunk.count("</blockquote>")


def test_practice_is_not_part_of_the_lesson_message() -> None:
    lesson = Lesson.model_validate(lesson_payload())
    text = "\n".join(render_messages(lesson))
    assert lesson.quiz[0].question not in text
    assert lesson.translation[0].ru not in text


def test_quiz_summary_lists_only_the_wrong_answers() -> None:
    lesson = Lesson.model_validate(lesson_payload())
    text = render_quiz_summary([(lesson.quiz[0], True), (lesson.quiz[1], False), (lesson.quiz[2], True)])
    assert "Итог: 2/3" in text
    assert lesson.quiz[1].question in text and "There is milk in the fridge." in text
    assert lesson.quiz[0].question not in text


def test_grade_shows_fixes_and_escapes_learner_text() -> None:
    grade = TranslationGrade.model_validate(
        {
            "sentences": [
                {"n": 1, "learner_text": "a <b> c", "verdict": "correct", "corrected": "a <b> c", "explanation_ru": ""},
                {
                    "n": 2,
                    "learner_text": "There is no children.",
                    "verdict": "fixed",
                    "corrected": "There are no children.",
                    "explanation_ru": "Множественное число.",
                },
                {"n": 3, "learner_text": "", "verdict": "missing", "corrected": "Is there?", "explanation_ru": ""},
            ],
            "focus_ru": "Число после there.",
        }
    )
    text = render_grade(grade)
    assert "Результат: 1/3" in text
    assert "a &lt;b&gt; c" in text
    assert "→ <b>There are no children.</b>" in text
    assert "нет ответа" in text
    assert "Число после there." in text
