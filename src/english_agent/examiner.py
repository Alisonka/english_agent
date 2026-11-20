"""The Examiner: an exam on a block of studied topics, run by code and reported by a model.

Preparation: the pool holds the topics' bank questions the learner has never answered, so the exam
tests the construction, not the memory of a question. The exam itself is code: one quiz poll at a
time, the next chosen adaptively from the running ability estimate (`english_agent.adaptive`), until
every topic is settled or the limit is reached. Scoring is code too: a verdict per topic from its
answers. The model (EXAMINER_MODEL) only writes the short summary and one recommendation per topic.
Mastered topics are marked "confirmed"; when the whole level is known, the learner is offered the
next level and confirms it with a button. Every answer is evidence and moves the ability.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from english_agent.adaptive import AdaptiveTest, Candidate
from english_agent.knowledge import question_key, start_ability, start_difficulty, topic_meta
from english_agent.llm import ChatLLM, LLMError, LLMWriteError, submit_model, tool_schema
from english_agent.models.cefr import CEFR_LEVELS
from english_agent.models.curriculum import all_topics, load_curriculum
from english_agent.models.exam import ExamRecord, ExamReport, ExamReportDraft, PoolQuestion, TopicResult, verdict_for
from english_agent.models.practice import QuizQuestion
from english_agent.models.storage import LearnerRecord
from english_agent.storage import Stores
from english_agent.telegram.sender import Sender, TelegramError
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

log = logging.getLogger(__name__)

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "exam_report_system.md").read_text(encoding="utf-8")
QUESTIONS_PER_TOPIC = 4
MAX_QUESTIONS = 25
LEVEL_PREFIX = "level:"  # the bot's level button (bot.handlers.LEVEL_PREFIX): the learner confirms the move
VERDICT_RU = {"mastered": "освоено", "shaky": "шатко", "not_mastered": "не освоено"}


async def exam_pool(stores: Stores, learner: LearnerRecord, topic_ids: list[str]) -> list[PoolQuestion]:
    """The topics' bank questions the learner never answered, with their current difficulty."""
    sent = await stores.lessons.sent_steps(learner.id, learner.cefr)
    seen = await stores.evidence.question_keys(learner.id)
    found: dict[str, PoolQuestion] = {}
    for topic_id in topic_ids:
        content_ids = {step.content_id for step in sent if step.topic_id == topic_id and step.content_id is not None}
        for content_id in content_ids:
            content = await stores.contents.get(content_id)
            if content is None or content.topic is None:
                continue
            for step in content.topic.steps:
                for question in step.practice.quiz if step.practice is not None else []:
                    item_key = f"{topic_id}:{question.item_slug}"
                    key = question_key(item_key, question.question)
                    if key in seen or key in found:
                        continue
                    found[key] = PoolQuestion(
                        question_key=key,
                        topic_id=topic_id,
                        item_id=None,
                        item_key=item_key,
                        difficulty=start_difficulty(topic_meta(topic_id), "quiz"),
                        question=QuizQuestion(**question.model_dump(exclude={"item_slug"})),
                    )
    stats = await stores.knowledge.questions(list(found))
    ids = await stores.items.ids_for_keys(sorted({q.item_key for q in found.values()}))
    return [
        q.model_copy(
            update={
                "difficulty": stats[q.question_key].difficulty if q.question_key in stats else q.difficulty,
                "item_id": ids.get(q.item_key),
            }
        )
        for q in found.values()
    ]


async def start_exam(
    stores: Stores, sender: Sender, learner: LearnerRecord, topic_ids: list[str], day: date
) -> ExamRecord | None:
    """Prepare and open the day's exam with its first question; None when it was started already."""
    pool = await exam_pool(stores, learner, topic_ids)
    if not pool:
        log.warning("exam for learner %s: no unseen questions on %s", learner.id, topic_ids)
        return None
    ability = await stores.knowledge.ability(learner.id)
    exam = await stores.exams.create(
        learner.id, day, topic_ids, ability.overall if ability is not None else start_ability(learner.cefr), pool
    )
    if exam is None:
        return None
    titles = all_topics()
    names = ", ".join(titles[t].title_ru for t in topic_ids if t in titles)
    await sender.send_text(learner.telegram_chat_id, f"Сегодня экзамен по темам: {html.escape(names)}. Поехали!")
    await continue_exam(stores, sender, exam.id, None)
    return exam


def _test(exam: ExamRecord) -> tuple[AdaptiveTest, list[Candidate]]:
    pool = [Candidate(q.question_key, q.topic_id, q.difficulty) for q in exam.pool]
    by_key = {c.question_key: c for c in pool}
    test = AdaptiveTest(
        ability=exam.ability_start,
        topics=exam.topic_ids,
        max_per_topic=QUESTIONS_PER_TOPIC,
        max_total=min(MAX_QUESTIONS, QUESTIONS_PER_TOPIC * len(exam.topic_ids)),
    )
    for answer in exam.answers:
        test.record(by_key[answer.question_key], answer.correct)
    return test, pool


async def continue_exam(stores: Stores, sender: Sender, exam_id: int, writer: ChatLLM | None) -> str:
    """After an answer: send the next question, or score and report the exam. Returns what happened."""
    exam = await stores.exams.get(exam_id)
    if exam is None or exam.state != "running":
        return "none"
    learner = await stores.learners.get_by_id(exam.learner_id)
    if learner is None:
        return "none"
    if await stores.exams.has_open_poll(exam.id):
        return "waiting"  # one question at a time: the last one is not answered yet
    test, pool = _test(exam)
    nxt = test.next(pool)
    if nxt is not None:
        question = next(q for q in exam.pool if q.question_key == nxt.question_key).question
        _, poll_id = await sender.send_poll(
            learner.telegram_chat_id,
            question.question,
            question.options,
            correct_option=question.correct_option,
            explanation=question.explanation_ru,
        )
        await stores.exams.add_poll(exam.id, poll_id, nxt.question_key)
        return "asked"
    await finish_exam(stores, sender, exam, learner, writer)
    return "finished"


def score(exam: ExamRecord) -> list[TopicResult]:
    results = []
    for topic_id in exam.topic_ids:
        marks = [a.correct for a in exam.answers if a.topic_id == topic_id]
        results.append(
            TopicResult(
                topic_id=topic_id, answers=len(marks), correct=sum(marks), verdict=verdict_for(sum(marks), len(marks))
            )
        )
    return results


async def finish_exam(
    stores: Stores, sender: Sender, exam: ExamRecord, learner: LearnerRecord, writer: ChatLLM | None
) -> ExamReport:
    now = datetime.now(UTC)
    results = score(exam)
    draft = await _draft(writer, exam, results)
    report = ExamReport(
        summary_ru=draft.summary_ru,
        topics=[
            r.model_copy(update={"recommendation_ru": draft.recommendations.get(r.topic_id, "")[:200]}) for r in results
        ],
        provenance=build_provenance(
            component="examiner", schema_version=SCHEMA_VERSIONS["exam_report"], model=getattr(writer, "model", None)
        ),
    )
    await stores.exams.finish(exam.id, report, now)
    mastered = [r.topic_id for r in results if r.verdict == "mastered"]
    await stores.knowledge.mark_topics(learner.id, mastered, "confirmed", source_ref=f"exam:{exam.id}", at=now)
    try:
        await sender.send_text(
            learner.telegram_chat_id, render_report(report), reply_markup=await _next_level(stores, learner, now)
        )
    except TelegramError as error:
        log.warning("could not deliver the exam report %s: %s", exam.id, error)
    return report


async def _draft(writer: ChatLLM | None, exam: ExamRecord, results: list[TopicResult]) -> ExamReportDraft:
    plain = ExamReportDraft(
        summary_ru=f"Экзамен окончен: освоено тем — {sum(r.verdict == 'mastered' for r in results)} из {len(results)}."
    )
    if writer is None:
        return plain
    titles = all_topics()
    lines = [
        f"- {r.topic_id} ({titles[r.topic_id].title_ru if r.topic_id in titles else r.topic_id}): "
        f"{r.correct}/{r.answers}, verdict {r.verdict}"
        for r in results
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": "Results (verdicts are computed by code):\n" + "\n".join(lines)},
    ]
    try:
        return await submit_model(
            writer,
            ExamReportDraft,
            tool=tool_schema(
                ExamReportDraft, name="submit_report", description="Submit the summary and recommendations."
            ),
            messages=messages,
            build=lambda arguments: arguments,
            span_name="examiner.report",
            span_attrs={"exam.id": exam.id},
        )
    except (LLMError, LLMWriteError) as error:
        log.warning("exam report %s written by template: %s", exam.id, error)
        return plain


def render_report(report: ExamReport) -> str:
    titles = all_topics()
    lines = [f"<b>Итоги экзамена</b>\n{html.escape(report.summary_ru)}"]
    for r in report.topics:
        name = titles[r.topic_id].title_ru if r.topic_id in titles else r.topic_id
        tip = f" — {html.escape(r.recommendation_ru)}" if r.recommendation_ru else ""
        lines.append(f"• {html.escape(name)}: {VERDICT_RU[r.verdict]} ({r.correct}/{r.answers}){tip}")
    return "\n".join(lines)


async def _next_level(stores: Stores, learner: LearnerRecord, now: datetime) -> InlineKeyboardMarkup | None:
    """The next level's button when the whole level is known: moving up is the learner's choice."""
    index = CEFR_LEVELS.index(learner.cefr)
    if index + 1 >= len(CEFR_LEVELS):
        return None
    known = await stores.knowledge.known_topics(learner.id, now)
    if not {topic.id for topic in load_curriculum(learner.cefr).topics} <= known:
        return None
    level = CEFR_LEVELS[index + 1]
    button = InlineKeyboardButton(text=f"Перейти на {level}", callback_data=f"{LEVEL_PREFIX}{level}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
