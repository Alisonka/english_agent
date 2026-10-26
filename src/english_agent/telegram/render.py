"""Render a validated `Lesson` as Telegram HTML.

Telegram's HTML parse mode only needs `<`, `>` and `&` escaped, which is far more
robust for LLM-written text than MarkdownV2 (a dozen reserved characters). Every
line the renderer emits is tag-balanced, so `split_message` can break a long
lesson on paragraph / line boundaries without ever producing an unclosed tag.
"""

from __future__ import annotations

import html

from english_agent.models.lesson import TELEGRAM_MAX_MESSAGE_LENGTH, Example, Lesson

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


def render_blocks(lesson: Lesson) -> list[str]:
    """The lesson as self-contained HTML blocks, in reading order."""
    header = f"<b>{_e(lesson.title_en)}</b>\n{_e(lesson.title_ru)} · {lesson.cefr}"
    form = f"<b>Формула</b>\n<code>{_e(lesson.form)}</code>"
    rule = f"<b>Правило</b>\n{_e(lesson.rule_ru)}"
    nuances = "<b>Нюансы</b>\n" + "\n".join(f"• {_e(item)}" for item in lesson.nuances_ru)
    examples = "<b>Примеры</b>\n" + "\n".join(
        f"{number}. {highlight(example)}\n   <i>{_e(example.ru)}</i>"
        for number, example in enumerate(lesson.examples, start=1)
    )
    mistake = (
        f"<b>Типичная ошибка</b>\n{_e(lesson.mistake_ru)}\n✗ <s>{_e(lesson.incorrect)}</s>\n✓ {_e(lesson.correct)}"
    )
    sources = "<b>Источники</b>\n" + "\n".join(
        f'• <a href="{html.escape(str(source.url), quote=True)}">{_e(source.title)}</a>' for source in lesson.sources
    )
    return [header, form, rule, nuances, examples, mistake, sources]


def render_lesson(lesson: Lesson) -> str:
    return PARAGRAPH.join(render_blocks(lesson))


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
