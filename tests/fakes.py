"""In-memory stand-ins for the storage repositories, for tests that need no database."""

from __future__ import annotations

from datetime import UTC, date, datetime

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.storage import (
    LearnerRecord,
    LessonRecord,
    OpenTranslationTask,
    PracticeDeliveryRecord,
    PracticeKind,
    QuizPollRecord,
    QuizResult,
    TranslationAnswerRecord,
)
from english_agent.storage.repositories import LessonAlreadySentError, Stores
from english_agent.versions import SCHEMA_VERSIONS


class FakeLearners:
    def __init__(self) -> None:
        self.by_chat: dict[str, LearnerRecord] = {}

    def get(self, telegram_chat_id: str) -> LearnerRecord | None:
        return self.by_chat.get(telegram_chat_id)

    def get_or_create(self, telegram_chat_id: str, default_cefr: Cefr) -> LearnerRecord:
        if telegram_chat_id not in self.by_chat:
            now = datetime.now(UTC)
            self.by_chat[telegram_chat_id] = LearnerRecord(
                id=len(self.by_chat) + 1,
                telegram_chat_id=telegram_chat_id,
                cefr=default_cefr,
                created_at=now,
                updated_at=now,
            )
        return self.by_chat[telegram_chat_id]

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord:
        learner = self.get_or_create(telegram_chat_id, cefr).model_copy(update={"cefr": cefr})
        self.by_chat[telegram_chat_id] = learner
        return learner


class FakeLessons:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, date], LessonRecord] = {}

    def get(self, learner_id: int, day: date) -> LessonRecord | None:
        return self.rows.get((learner_id, day))

    def sent_on(self, learner_id: int, day: date) -> bool:
        row = self.rows.get((learner_id, day))
        return row is not None and row.status == "sent"

    def record_sent(
        self, learner_id: int, day: date, lesson: Lesson, message_ids: list[int], *, curriculum_version: int | None
    ) -> LessonRecord:
        return self._save(
            learner_id,
            day,
            topic_id=lesson.topic_id,
            cefr=lesson.cefr,
            curriculum_version=curriculum_version,
            lesson=lesson,
            error=None,
            message_ids=message_ids,
        )

    def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord:
        return self._save(
            learner_id,
            day,
            topic_id=topic_id,
            cefr=cefr,
            curriculum_version=curriculum_version,
            lesson=None,
            error=reason,
            message_ids=[],
        )

    def _save(
        self,
        learner_id: int,
        day: date,
        *,
        topic_id: str,
        cefr: Cefr,
        curriculum_version: int | None,
        lesson: Lesson | None,
        error: str | None,
        message_ids: list[int],
    ) -> LessonRecord:
        if self.sent_on(learner_id, day):
            raise LessonAlreadySentError(f"{learner_id} {day}")
        now = datetime.now(UTC)
        record = LessonRecord(
            id=len(self.rows) + 1,
            learner_id=learner_id,
            lesson_date=day,
            topic_id=topic_id,
            cefr=cefr,
            curriculum_version=curriculum_version,
            schema_version=SCHEMA_VERSIONS["lesson"],
            status="sent" if lesson is not None else "failed",
            lesson=lesson,
            error=error,
            telegram_message_ids=message_ids,
            created_at=now,
            sent_at=now if lesson is not None else None,
        )
        self.rows[(learner_id, day)] = record
        return record


class FakePractice:
    def __init__(self, learners: FakeLearners, lessons: FakeLessons) -> None:
        self._learners, self._lessons = learners, lessons
        self.deliveries: dict[int, PracticeDeliveryRecord] = {}
        self.polls: dict[str, QuizPollRecord] = {}
        self.poll_answers: dict[tuple[int, str], bool] = {}  # (poll id, chat id) -> correct
        self.translation_answers: dict[int, TranslationAnswerRecord] = {}
        self.errors: list[tuple[str, str, str]] = []  # (kind, original, corrected)

    def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None:
        return next((d for d in self.deliveries.values() if d.lesson_id == lesson_id and d.kind == kind), None)

    def claim_delivery(self, lesson_id: int, kind: PracticeKind, item_count: int) -> PracticeDeliveryRecord | None:
        if self.delivery(lesson_id, kind) is not None:
            return None
        record = PracticeDeliveryRecord(
            id=len(self.deliveries) + 1,
            lesson_id=lesson_id,
            kind=kind,
            item_count=item_count,
            claimed_at=datetime.now(UTC),
            sent_at=None,
            telegram_message_ids=[],
            completed_at=None,
        )
        self.deliveries[record.id] = record
        return record

    def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord:
        record = self.deliveries[delivery_id].model_copy(
            update={"sent_at": datetime.now(UTC), "telegram_message_ids": message_ids}
        )
        self.deliveries[delivery_id] = record
        return record

    def add_poll(
        self,
        delivery_id: int,
        *,
        position: int,
        telegram_poll_id: str,
        telegram_message_id: int,
        question: QuizQuestion,
    ) -> QuizPollRecord:
        record = QuizPollRecord(
            id=len(self.polls) + 1,
            delivery_id=delivery_id,
            position=position,
            telegram_poll_id=telegram_poll_id,
            telegram_message_id=telegram_message_id,
            question=question,
            created_at=datetime.now(UTC),
        )
        self.polls[telegram_poll_id] = record
        return record

    def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        poll = self.polls.get(telegram_poll_id)
        if poll is None or self._learners.get(telegram_chat_id) is None:
            return None
        if (poll.id, telegram_chat_id) in self.poll_answers:
            return None
        question = poll.question
        is_correct = option_ids == [question.correct_option]
        self.poll_answers[(poll.id, telegram_chat_id)] = is_correct
        if not is_correct:
            chosen = ", ".join(question.options[i] for i in option_ids)
            self.errors.append(("quiz", chosen, question.options[question.correct_option]))
        delivery = self.deliveries[poll.delivery_id]
        answered = sorted(
            (
                p
                for p in self.polls.values()
                if p.delivery_id == delivery.id and (p.id, telegram_chat_id) in self.poll_answers
            ),
            key=lambda p: p.position,
        )
        if delivery.completed_at is not None or len(answered) < delivery.item_count:
            return QuizResult(is_correct=is_correct, completed=False)
        self.deliveries[delivery.id] = delivery.model_copy(update={"completed_at": datetime.now(UTC)})
        return QuizResult(
            is_correct=is_correct,
            completed=True,
            results=[(p.question, self.poll_answers[(p.id, telegram_chat_id)]) for p in answered],
        )

    def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None:
        learner = self._learners.get(telegram_chat_id)
        if learner is None:
            return None
        lessons = {r.id: r for (learner_id, _), r in self._lessons.rows.items() if learner_id == learner.id}
        sent = [
            d
            for d in self.deliveries.values()
            if d.kind == "translation" and d.sent_at is not None and d.lesson_id in lessons
        ]
        if not sent:
            return None
        delivery = max(sent, key=lambda d: d.sent_at or d.claimed_at)
        answer = next((a for a in self.translation_answers.values() if a.delivery_id == delivery.id), None)
        lesson = lessons[delivery.lesson_id].lesson
        if (answer is not None and answer.status != "failed") or lesson is None:
            return None
        return OpenTranslationTask(learner_id=learner.id, delivery=delivery, lesson=lesson)

    def start_translation_answer(
        self, delivery_id: int, learner_id: int, answer_text: str
    ) -> TranslationAnswerRecord | None:
        existing = next((a for a in self.translation_answers.values() if a.delivery_id == delivery_id), None)
        if existing is not None and existing.status != "failed":
            return None
        record = TranslationAnswerRecord(
            id=existing.id if existing is not None else len(self.translation_answers) + 1,
            delivery_id=delivery_id,
            learner_id=learner_id,
            answer_text=answer_text,
            status="pending",
            grade=None,
            error=None,
            progress_message_id=None,
            disputed=False,
            created_at=datetime.now(UTC),
            graded_at=None,
        )
        self.translation_answers[record.id] = record
        return record

    def set_progress_message(self, answer_id: int, message_id: int) -> None:
        self._update_answer(answer_id, progress_message_id=message_id)

    def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord:
        record = self._update_answer(answer_id, status="graded", grade=grade, graded_at=datetime.now(UTC))
        self.errors.extend(("text", s.learner_text, s.corrected) for s in grade.sentences if s.verdict == "fixed")
        return record

    def fail_translation_answer(self, answer_id: int, reason: str) -> None:
        self._update_answer(answer_id, status="failed", error=reason)

    def dispute(self, answer_id: int) -> bool:
        answer = self.translation_answers.get(answer_id)
        if answer is None or answer.status != "graded":
            return False
        self._update_answer(answer_id, disputed=True)
        return True

    def _update_answer(self, answer_id: int, **changes: object) -> TranslationAnswerRecord:
        record = self.translation_answers[answer_id].model_copy(update=changes)
        self.translation_answers[answer_id] = record
        return record


def fake_stores() -> tuple[Stores, FakeLearners, FakeLessons]:
    learners, lessons = FakeLearners(), FakeLessons()
    return Stores(learners=learners, lessons=lessons, practice=FakePractice(learners, lessons)), learners, lessons


def fake_practice(stores: Stores) -> FakePractice:
    assert isinstance(stores.practice, FakePractice)
    return stores.practice
