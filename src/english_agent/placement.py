"""The placement test: 15 to 25 adaptive questions that turn into a knowledge map, not just a letter.

Questions come from the written topics of every level, grouped by the topic's first tag so the test
covers tenses, articles, modals and so on. Code picks each next one from the running ability
estimate (the exam's adaptive selector), starting at medium difficulty, and stops when the estimate
settles or at 25 questions. A level whose questions the learner would answer right at least 4 times
in 5 is credited: its topics are marked "credited" (honestly: credited without a check; a later
probe the learner gets wrong puts a topic back on the route). The learner's level becomes the first
level not credited; the frontier starts there.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime

from english_agent.adaptive import AdaptiveTest, Candidate
from english_agent.knowledge import LEVEL_DIFFICULTY, probability, question_key, start_difficulty, topic_meta
from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import load_curriculum
from english_agent.models.exam import ExamRecord, ExamReport, PoolQuestion, TopicResult, verdict_for
from english_agent.models.practice import QuizQuestion
from english_agent.models.storage import LearnerRecord
from english_agent.storage import Stores
from english_agent.telegram.sender import Sender

MIN_QUESTIONS = 15
MAX_QUESTIONS = 25
START_ABILITY = 0.0  # medium difficulty: the B1 questions
CREDIT_AT = 0.8  # predicted chance on a level's questions to credit the level
INTRO = f"Небольшой тест: {MIN_QUESTIONS}–{MAX_QUESTIONS} вопросов, чтобы понять, с чего начать."
UNAVAILABLE = "Тест пока недоступен: выбери уровень кнопками — /level."


def group_of(topic_id: str) -> str:
    meta = topic_meta(topic_id)
    return meta.tags[0] if meta is not None and meta.tags else "other"


async def placement_pool(stores: Stores, content_key: str) -> list[PoolQuestion]:
    """The quiz questions of every written topic, grouped by tag, at their calibrated difficulty."""
    found: dict[str, PoolQuestion] = {}
    for record in await stores.contents.ready_contents(content_key):
        content = record.topic
        if content is None:
            continue
        for step in content.steps:
            for question in step.practice.quiz if step.practice is not None else []:
                item_key = f"{step.topic_id}:{question.item_slug}"
                key = question_key(item_key, question.question)
                found.setdefault(
                    key,
                    PoolQuestion(
                        question_key=key,
                        topic_id=step.topic_id,
                        item_id=None,
                        item_key=item_key,
                        difficulty=start_difficulty(topic_meta(step.topic_id), "quiz"),
                        question=QuizQuestion(**question.model_dump(exclude={"item_slug"})),
                    ),
                )
    stats = await stores.knowledge.questions(list(found))
    return [
        q.model_copy(update={"difficulty": stats[q.question_key].difficulty}) if q.question_key in stats else q
        for q in found.values()
    ]


def placement_test(exam: ExamRecord) -> AdaptiveTest:
    """The adaptive test over tag groups (the exam's topics are groups here)."""
    return AdaptiveTest(
        ability=exam.ability_start,
        topics=exam.topic_ids,
        min_per_topic=1,
        max_per_topic=4,
        min_total=MIN_QUESTIONS,
        max_total=MAX_QUESTIONS,
    )


async def start_placement(
    stores: Stores, sender: Sender, learner: LearnerRecord, day: date, content_key: str
) -> ExamRecord | None:
    """Open the placement test (the exam's loop then asks its questions); None when it cannot run."""
    pool = [q.model_copy(update={"topic_id": group_of(q.topic_id)}) for q in await placement_pool(stores, content_key)]
    groups = sorted({q.topic_id for q in pool})
    if len(pool) < MIN_QUESTIONS:
        await sender.send_text(learner.telegram_chat_id, UNAVAILABLE)
        return None
    exam = await stores.exams.create(learner.id, day, groups, START_ABILITY, pool, kind="placement")
    if exam is not None:
        await sender.send_text(learner.telegram_chat_id, INTRO)
    return exam


def place(ability: float) -> tuple[Cefr, list[Cefr]]:
    """The learner's level and the levels credited below it."""
    credited = [cefr for cefr in CEFR_LEVELS if probability(ability, [], LEVEL_DIFFICULTY[cefr]) >= CREDIT_AT]
    below = [cefr for cefr in CEFR_LEVELS if cefr not in credited]
    level = below[0] if below else CEFR_LEVELS[-1]
    return level, [cefr for cefr in credited if CEFR_LEVELS.index(cefr) < CEFR_LEVELS.index(level)]


async def finish_placement(stores: Stores, sender: Sender, exam: ExamRecord, learner: LearnerRecord) -> ExamReport:
    now = datetime.now(UTC)
    test = placement_test(exam)
    pool = {q.question_key: q for q in exam.pool}
    for answer in exam.answers:
        question = pool[answer.question_key]
        test.record(Candidate(question.question_key, question.topic_id, question.difficulty), answer.correct)
    level, credited = place(test.ability)
    credited_topics = [topic.id for cefr in credited for topic in load_curriculum(cefr).topics]
    ref = f"placement:{exam.id}"
    await stores.knowledge.mark_topics(learner.id, credited_topics, "credited", source_ref=ref, at=now)
    await stores.learners.set_cefr(learner.telegram_chat_id, level)
    groups = []
    for group in exam.topic_ids:
        marks = [correct for candidate, correct in test.answers if candidate.topic_id == group]
        if marks:
            verdict = verdict_for(sum(marks), len(marks))
            groups.append(TopicResult(topic_id=group, answers=len(marks), correct=sum(marks), verdict=verdict))
    summary = f"Твой уровень — {level}."
    if credited:
        summary += f" Темы уровней {', '.join(credited)} зачтены без проверки; шаткая тема вернётся в маршрут."
    report = ExamReport(summary_ru=summary, topics=groups)
    await stores.exams.finish(exam.id, report, now)
    await sender.send_text(learner.telegram_chat_id, html.escape(summary) + " Поменять уровень можно командой /level.")
    return report
