"""The Arbiter: a stronger, independent second opinion on a disputed translation grade.

The learner taps "Не согласна с оценкой". The Arbiter (ARBITER_MODEL, high effort) grades the answer
again without seeing the first verdict or the Diagnostician's notes; code compares the two. A
sentence the first check corrected and the Arbiter accepts is overturned, and the data is
compensated: the error and the evidence are cancelled, the Elo move is taken back, and the card is
rebuilt as if the rating had not happened. Otherwise the learner gets the Arbiter's fuller
explanation. One dispute per answer; the share of overturned grades measures the grader.

The Arbiter is a stronger opinion, not the ground truth; it never lowers a grade.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime
from typing import Literal

from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.grader import grade_translation
from english_agent.grading import Grader
from english_agent.llm import AnthropicLLM
from english_agent.metrics import ARBITRATIONS
from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import GradingInput
from english_agent.storage import Stores
from english_agent.telegram.sender import Sender, TelegramError

log = logging.getLogger(__name__)

ArbitrationOutcome = Literal["none", "upheld", "overturned"]


def make_arbiter(settings: Settings, client: AsyncAnthropic | None = None) -> Grader | None:
    """Grade disputes with ARBITER_MODEL; None when the Arbiter is off (empty ARBITER_MODEL)."""
    if not settings.arbiter_model:
        return None
    llm = AnthropicLLM(settings, model=settings.arbiter_model, effort=settings.arbiter_effort, client=client)

    async def grade(task: GradingInput, text: str) -> TranslationGrade:
        return await grade_translation(llm, task, text)

    return grade


def overturned_sentences(first: TranslationGrade, second: TranslationGrade) -> list[int]:
    """Sentences the first check corrected that the Arbiter accepts as they are."""
    accepted = {sentence.n for sentence in second.sentences if sentence.verdict == "correct"}
    return sorted(sentence.n for sentence in first.sentences if sentence.verdict == "fixed" and sentence.n in accepted)


def render_ruling(second: TranslationGrade, overturned: list[int]) -> str:
    if overturned:
        numbers = ", ".join(str(n) for n in overturned)
        return f"✅ Независимая проверка пересмотрела оценку: предложения {numbers} засчитаны как верные."
    lines = ["Независимая проверка согласна с оценкой. Подробнее:"]
    for sentence in sorted(second.sentences, key=lambda item: item.n):
        if sentence.verdict == "fixed":
            lines.append(f"{sentence.n}. {html.escape(sentence.corrected)} — {html.escape(sentence.explanation_ru)}")
    return "\n".join(lines)


async def arbitrate(stores: Stores, sender: Sender, arbiter: Grader, answer_id: int) -> ArbitrationOutcome:
    """Rule on one disputed answer; "none" when there is nothing to rule on (or it was ruled already)."""
    task = await stores.practice.for_arbitration(answer_id)
    if task is None or task.answer.grade is None:
        return "none"
    independent = task.input.model_copy(update={"learner": None})  # the Arbiter does not see the notes
    second = await arbiter(independent, task.answer.answer_text)
    overturned = overturned_sentences(task.answer.grade, second)
    undone = await stores.practice.overturn(answer_id, second, overturned, datetime.now(UTC))
    outcome: ArbitrationOutcome = "overturned" if undone else "upheld"
    ARBITRATIONS.labels(outcome).inc()
    try:
        await sender.send_text(task.telegram_chat_id, render_ruling(second, undone))
    except TelegramError as error:
        log.warning("could not deliver the ruling on answer %s: %s", answer_id, error)
    if undone:
        log.info("answer %s: the Arbiter overturned sentences %s", answer_id, undone)
    return outcome
