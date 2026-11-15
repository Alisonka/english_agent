"""Render a validated `Lesson` as Telegram HTML.

Telegram's HTML parse mode only needs `<`, `>` and `&` escaped, which is far more
robust for LLM-written text than MarkdownV2 (a dozen reserved characters).

The lesson is emitted as a list of self-contained, tag-balanced blocks. The rule
sits in a multi-line `<blockquote expandable>`, so a message can never be cut
inside it: `render_messages` packs whole blocks and only ever line-splits the
line-safe blocks (in practice just the examples at the field bounds).
"""

from __future__ import annotations

import html
import re

from english_agent.models.lesson import TELEGRAM_MAX_MESSAGE_LENGTH, Example, Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade, TranslationTask
from english_agent.models.profile import TopicWeakPoint
from english_agent.models.topic import LessonStep, StepExample, TopicSummary

PARAGRAPH = "\n\n"
_SPLIT_SEPARATORS = (PARAGRAPH, "\n")


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def highlight(example: Example) -> str:
    """`example.en` with every occurrence of every highlight wrapped in <b>.

    Overlapping or adjacent spans are merged first, so highlights such as
    ["There is", "is"] never produce nested or interleaved tags.
    """
    spans: list[tuple[int, int]] = []
    for part in example.highlights:
        start = example.en.find(part)
        while start != -1:
            spans.append((start, start + len(part)))
            start = example.en.find(part, start + 1)

    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    out: list[str] = []
    cursor = 0
    for start, end in merged:
        out.append(_e(example.en[cursor:start]))
        out.append(f"<b>{_e(example.en[start:end])}</b>")
        cursor = end
    out.append(_e(example.en[cursor:]))
    return "".join(out)


def _example_line(example: Example) -> str:
    # Compact: construction and translation on one scannable line.
    marker = "❓" if example.en.rstrip().endswith("?") else "✅"
    return f"{marker} {highlight(example)} — <i>{_e(example.ru)}</i>"


# A short leading label like "Формы." or "Вопрос." at the start of a rule paragraph.
_RULE_LABEL = re.compile(r"^(\S[^.\n]{0,24})\.(\s+)(\S)")


def _rule_paragraph(paragraph: str) -> str:
    """Escape a rule paragraph, bolding a short leading `Label.` if present."""
    paragraph = paragraph.strip()
    match = _RULE_LABEL.match(paragraph)
    if not match:
        return _e(paragraph)
    label, gap = match.group(1), match.group(2)
    rest = paragraph[match.start(3) :]
    return f"<b>{_e(label)}.</b>{gap}{_e(rest)}"


def _rule_body(rule_ru: str) -> str:
    """The rule's paragraphs, with air between them and bold mini-labels, for the quote."""
    paragraphs = [p for p in re.split(r"\n{2,}", rule_ru.strip()) if p.strip()]
    return PARAGRAPH.join(_rule_paragraph(p) for p in paragraphs)


def render_blocks(lesson: Lesson) -> list[str]:
    """The lesson as self-contained, tag-balanced HTML blocks, in reading order."""
    header = f"🇬🇧 <b>{_e(lesson.title_en)}</b>\n<i>{_e(lesson.title_ru)} · {lesson.cefr}</i>"
    form = f"📐 <code>{_e(lesson.form)}</code>"
    key_idea = f"💡 <b>Суть:</b> {_e(lesson.key_idea_ru)}"
    # The detailed theory lives in an expandable quote: compact when collapsed, airy when open.
    rule = f"📖 <b>Как это работает</b>\n<blockquote expandable>{_rule_body(lesson.rule_ru)}</blockquote>"
    examples = "📝 <b>Примеры</b>\n" + "\n".join(_example_line(example) for example in lesson.examples)
    nuances = "⚡️ <b>Запомни</b>\n" + "\n".join(f"🔸 {_e(item)}" for item in lesson.nuances_ru)
    mistake = (
        f"🚫 <b>Ошибка</b>\n❌ <s>{_e(lesson.incorrect)}</s> → ✅ {_e(lesson.correct)}\n<i>{_e(lesson.mistake_ru)}</i>"
    )
    sources = "🔗 <b>Источники</b>\n" + "\n".join(
        f'• <a href="{html.escape(str(source.url), quote=True)}">{_e(source.title)}</a>' for source in lesson.sources
    )
    return [header, form, key_idea, rule, examples, nuances, mistake, sources]


def render_lesson(lesson: Lesson) -> str:
    return PARAGRAPH.join(render_blocks(lesson))


def render_messages(lesson: Lesson, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Pack whole lesson blocks into messages of at most `limit` characters.

    Blocks are kept intact so the multi-line `<blockquote>` around the rule is
    never cut mid-tag. A block that alone exceeds `limit` — only ever the examples
    block at the field bounds — is split on paragraph / line boundaries, where
    every line is tag-balanced. The rule block is guaranteed to fit, so it is
    never line-split; we fail loudly rather than risk an unclosed quote.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    messages: list[str] = []
    current = ""
    for block in render_blocks(lesson):
        candidate = block if not current else f"{current}{PARAGRAPH}{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        if "<blockquote" in block:
            raise ValueError("rule block exceeds the message limit and cannot be split without breaking the quote")
        pieces = _split(block, limit, _SPLIT_SEPARATORS)
        messages.extend(pieces[:-1])
        current = pieces[-1]
    if current:
        messages.append(current)
    return messages


def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split `text` into chunks of at most `limit` characters.

    Breaks on paragraph boundaries first, then on single newlines. A single line
    longer than `limit` is cut hard as a last resort; with the `Lesson` field
    bounds that cannot happen for rendered lessons.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    return _split(text, limit, _SPLIT_SEPARATORS)


def _split(text: str, limit: int, separators: tuple[str, ...]) -> list[str]:
    if len(text) <= limit:
        return [text]
    if not separators:
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    separator, rest = separators[0], separators[1:]
    chunks: list[str] = []
    current = ""
    for part in text.split(separator):
        pieces = _split(part, limit, rest)
        if len(pieces) > 1:
            # `part` alone overflowed; its pieces are already as full as they can be.
            if current:
                chunks.append(current)
            chunks.extend(pieces[:-1])
            current = pieces[-1]
            continue
        piece = pieces[0]
        candidate = piece if not current else f"{current}{separator}{piece}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


# --- practice sent after the lesson ------------------------------------------------

_VERDICT_MARKS = {"correct": "✅", "fixed": "✏️", "missing": "➖"}


def render_quiz_intro(title_en: str, count: int) -> str:
    return f"🧠 <b>Проверь себя:</b> {_e(title_en)} · {count} вопроса"


def render_quiz_summary(results: list[tuple[QuizQuestion, bool]]) -> str:
    correct = sum(ok for _, ok in results)
    lines = [f"<b>Итог: {correct}/{len(results)}</b> {'🎉' if correct == len(results) else '✅'}"]
    for question, ok in results:
        if not ok:
            answer = question.options[question.correct_option]
            lines.append(f"❌ {_e(question.question)}\n→ <b>{_e(answer)}</b>")
    lines.append("Вечером будет перевод на эту тему.")
    return PARAGRAPH.join(lines)


def render_translation_task(title_en: str, tasks: list[TranslationTask]) -> str:
    sentences = "\n".join(f"{n}. {_e(task.ru)}" for n, task in enumerate(tasks, start=1))
    return (
        f"✍️ <b>Переведи на английский</b> · {_e(title_en)}\n\n{sentences}\n\n"
        "Ответь одним сообщением, каждое предложение с новой строки."
    )


def render_grade(grade: TranslationGrade) -> str:
    lines = [f"<b>Результат: {grade.correct_count}/{len(grade.sentences)}</b>"]
    for item in sorted(grade.sentences, key=lambda sentence: sentence.n):
        mark = _VERDICT_MARKS[item.verdict]
        if item.verdict == "correct":
            lines.append(f"{item.n}. {mark} {_e(item.learner_text)}")
            continue
        written = _e(item.learner_text) if item.learner_text else "<i>нет ответа</i>"
        block = f"{item.n}. {mark} {written}\n→ <b>{_e(item.corrected)}</b>"
        if item.explanation_ru:
            block += f"\n<i>{_e(item.explanation_ru)}</i>"
        lines.append(block)
    lines.append(f"🎯 <b>Завтра обрати внимание:</b> {_e(grade.focus_ru)}")
    return PARAGRAPH.join(lines)


# --- step and cheat sheet --------------------------------------------------------


def progress_bar(index: int, total: int) -> str:
    """A filled/empty bar for step `index` of `total`, e.g. ▰▰▱▱."""
    index = max(0, min(index, total))
    return "▰" * index + "▱" * (total - index)


def count_lines(text: str) -> int:
    """Non-empty lines of a rendered message; used to keep a step scannable."""
    return sum(1 for line in text.splitlines() if line.strip())


def _step_example_line(example: StepExample) -> str:
    # The English shows the construction in bold; the Russian hides behind a spoiler for self-check.
    return f"✅ {highlight(example)} — <tg-spoiler>{_e(example.ru)}</tg-spoiler>"


def render_step(step: LessonStep) -> str:
    """One step as a short, scannable Telegram HTML message."""
    message = step.message
    header = f"📘 <b>{_e(step.title_en)}</b> · шаг {step.index} из {step.total} {progress_bar(step.index, step.total)}"
    subtitle = f"<i>{_e(step.title_ru)}</i>" + (" · ещё раз, по-другому" if step.variant == "alt" else "")
    lines = [header, subtitle, "", f"❓ {_e(message.hook_ru)}", f"💡 <b>{_e(message.key_idea_ru)}</b>"]
    if message.form:
        lines.append(f"📐 <code>{_e(message.form)}</code>")
    lines.append("")
    lines.extend(_step_example_line(example) for example in message.examples)
    lines.append("")
    lines.append(f"🚫 <s>{_e(message.incorrect)}</s> → {_e(message.correct)}")
    lines.append(f"<i>{_e(message.contrast_ru)}</i>")
    check = message.self_check
    lines.append(
        f"✍️ {_e(check.prompt_en)} → <tg-spoiler>{_e(check.answer_en)} · {_e(check.explanation_ru)}</tg-spoiler>"
    )
    if message.teaser_ru:
        lines.append(f"⏭ <i>Завтра:</i> {_e(message.teaser_ru)}")
    return "\n".join(lines)


def render_cheat_sheet(
    summary: TopicSummary, weak_points: list[TopicWeakPoint], limit: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """The end-of-topic cheat sheet: the summary, the learner's weak points, and the sources.

    Split into several messages if it exceeds `limit`; every split is on a blank line or newline.
    """
    blocks = [f"📒 <b>Шпаргалка:</b> {_e(summary.title_en)}\n<i>{_e(summary.title_ru)}</i>"]
    for section in summary.sections:
        body = "\n".join(f"• {_e(line)}" for line in section.lines_ru)
        blocks.append(f"🔹 <b>{_e(section.title_ru)}</b>\n{body}")
    if summary.forms:
        blocks.append("📐 <b>Формы</b>\n" + "\n".join(f"• <code>{_e(form)}</code>" for form in summary.forms))
    if summary.top_mistakes_ru:
        blocks.append("🚫 <b>Частые ошибки</b>\n" + "\n".join(f"• {_e(item)}" for item in summary.top_mistakes_ru))
    blocks.append(_weak_points_block(weak_points))
    if summary.sources:
        sources = "\n".join(
            f'• <a href="{html.escape(str(source.url), quote=True)}">{_e(source.title)}</a>'
            for source in summary.sources
        )
        blocks.append(f"🔗 <b>Источники</b>\n{sources}")
    return split_message(PARAGRAPH.join(blocks), limit)


def _weak_points_block(weak_points: list[TopicWeakPoint]) -> str:
    if not weak_points:
        return "🎯 <b>Твои слабые места в этой теме</b>\nОшибок по теме не было — отлично!"
    lines = ["🎯 <b>Твои слабые места в этой теме</b>"]
    for point in weak_points:
        accuracy = f" ({round(point.accuracy * 100)}%)" if point.accuracy is not None else ""
        lines.append(f"• {_e(point.title_ru)}{accuracy} — {_e(point.key_idea_ru)}")
        if point.contrast_ru:
            lines.append(f"  💡 <i>{_e(point.contrast_ru)}</i>")
    return "\n".join(lines)
