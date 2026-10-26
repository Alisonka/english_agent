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


def _example_block(example: Example) -> str:
    marker = "❓" if example.en.rstrip().endswith("?") else "✅"
    return f"{marker} {highlight(example)}\n— <i>{_e(example.ru)}</i>"


def render_blocks(lesson: Lesson) -> list[str]:
    """The lesson as self-contained, tag-balanced HTML blocks, in reading order."""
    header = f"🇬🇧 <b>{_e(lesson.title_en)}</b>\n<i>{_e(lesson.title_ru)} · {lesson.cefr}</i>"
    form = f"📐 <b>Формула</b>\n<code>{_e(lesson.form)}</code>"
    # Collapse blank lines so the rule stays a single block with no `\n\n` to split on.
    rule_text = re.sub(r"\n{2,}", "\n", lesson.rule_ru.strip())
    rule = f"📖 <b>Правило</b>\n<blockquote expandable>{_e(rule_text)}</blockquote>"
    nuances = "⚡️ <b>Нюансы</b>\n" + "\n".join(f"🔸 {_e(item)}" for item in lesson.nuances_ru)
    examples = "📝 <b>Примеры</b>\n" + PARAGRAPH.join(_example_block(example) for example in lesson.examples)
    mistake = (
        "🚫 <b>Типичная ошибка</b>\n"
        f"❌ <s>{_e(lesson.incorrect)}</s>\n"
        f"✅ {_e(lesson.correct)}\n"
        f"<i>{_e(lesson.mistake_ru)}</i>"
    )
    sources = "🔗 <b>Источники</b>\n" + "\n".join(
        f'• <a href="{html.escape(str(source.url), quote=True)}">{_e(source.title)}</a>' for source in lesson.sources
    )
    return [header, form, rule, nuances, examples, mistake, sources]


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
