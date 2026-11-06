"""Repositories: the only code that touches ORM rows. They take and return pydantic records.

Each method runs in its own short transaction (see `session_scope`), so one repository
can be shared by the polling thread and the scheduler thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from english_agent.config import Settings
from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson, Source
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.storage import (
    CardRecord,
    ItemRecord,
    LearnerRecord,
    LessonRecord,
    OpenTranslationTask,
    PracticeDeliveryRecord,
    PracticeKind,
    QuizPollRecord,
    QuizResult,
    TranslationAnswerRecord,
)
from english_agent.storage.db import SessionFactory, make_engine, make_session_factory, session_scope
from english_agent.storage.orm import (
    CardRow,
    ErrorRow,
    ItemRow,
    LearnerRow,
    LessonRow,
    PracticeDeliveryRow,
    QuizAnswerRow,
    QuizPollRow,
    TranslationAnswerRow,
)
from english_agent.versions import SCHEMA_VERSIONS


class LessonAlreadySentError(Exception):
    """A lesson for this learner and day is already recorded as sent."""


class LearnerStore(Protocol):
    def get(self, telegram_chat_id: str) -> LearnerRecord | None: ...

    def get_or_create(self, telegram_chat_id: str, default_cefr: Cefr) -> LearnerRecord: ...

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord: ...


class LessonStore(Protocol):
    def get(self, learner_id: int, day: date) -> LessonRecord | None: ...

    def sent_on(self, learner_id: int, day: date) -> bool: ...

    def record_sent(
        self, learner_id: int, day: date, lesson: Lesson, message_ids: list[int], *, curriculum_version: int | None
    ) -> LessonRecord: ...

    def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord: ...


class PracticeStore(Protocol):
    def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None: ...

    def claim_delivery(self, lesson_id: int, kind: PracticeKind, item_count: int) -> PracticeDeliveryRecord | None: ...

    def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord: ...

    def add_poll(
        self,
        delivery_id: int,
        *,
        position: int,
        telegram_poll_id: str,
        telegram_message_id: int,
        question: QuizQuestion,
    ) -> QuizPollRecord: ...

    def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None: ...

    def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None: ...

    def start_translation_answer(
        self, delivery_id: int, learner_id: int, answer_text: str
    ) -> TranslationAnswerRecord | None: ...

    def set_progress_message(self, answer_id: int, message_id: int) -> None: ...

    def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord: ...

    def fail_translation_answer(self, answer_id: int, reason: str) -> None: ...

    def dispute(self, answer_id: int) -> bool: ...


@dataclass(frozen=True)
class Stores:
    learners: LearnerStore
    lessons: LessonStore
    practice: PracticeStore


def sql_stores(sessions: SessionFactory) -> Stores:
    return Stores(learners=LearnerRepo(sessions), lessons=LessonRepo(sessions), practice=PracticeRepo(sessions))


def open_stores(settings: Settings) -> Stores | None:
    """SQL-backed stores for DATABASE_URL, or None when it is not set."""
    if not settings.database_url.get_secret_value():
        return None
    return sql_stores(make_session_factory(make_engine(settings)))


class LearnerRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    def _find(session: Session, telegram_chat_id: str) -> LearnerRow | None:
        return session.scalar(select(LearnerRow).where(LearnerRow.telegram_chat_id == telegram_chat_id))

    def get(self, telegram_chat_id: str) -> LearnerRecord | None:
        with session_scope(self._sessions) as session:
            row = self._find(session, telegram_chat_id)
            return None if row is None else LearnerRecord.model_validate(row)

    def get_or_create(self, telegram_chat_id: str, default_cefr: Cefr) -> LearnerRecord:
        # ON CONFLICT: the polling and scheduler threads may both create the same learner.
        statement = (
            insert(LearnerRow)
            .values(telegram_chat_id=telegram_chat_id, cefr=default_cefr)
            .on_conflict_do_nothing(index_elements=[LearnerRow.telegram_chat_id])
        )
        with session_scope(self._sessions) as session:
            session.execute(statement)
            row = self._find(session, telegram_chat_id)
            assert row is not None
            return LearnerRecord.model_validate(row)

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord:
        """Set the learner's level, creating the learner if needed."""
        statement = insert(LearnerRow).values(telegram_chat_id=telegram_chat_id, cefr=cefr)
        statement = statement.on_conflict_do_update(
            index_elements=[LearnerRow.telegram_chat_id],
            set_={"cefr": statement.excluded.cefr, "updated_at": func.now()},
        )
        with session_scope(self._sessions) as session:
            session.execute(statement)
            row = self._find(session, telegram_chat_id)
            assert row is not None
            return LearnerRecord.model_validate(row)


class LessonRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    def _find(session: Session, learner_id: int, day: date) -> LessonRow | None:
        return session.scalar(select(LessonRow).where(LessonRow.learner_id == learner_id, LessonRow.lesson_date == day))

    def get(self, learner_id: int, day: date) -> LessonRecord | None:
        with session_scope(self._sessions) as session:
            row = self._find(session, learner_id, day)
            return None if row is None else LessonRecord.model_validate(row)

    def sent_on(self, learner_id: int, day: date) -> bool:
        with session_scope(self._sessions) as session:
            row = self._find(session, learner_id, day)
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
            status="sent",
            lesson=lesson.model_dump(mode="json"),
            error=None,
            telegram_message_ids=message_ids,
            sent_at=datetime.now(UTC),
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
            status="failed",
            lesson=None,
            error=reason,
            telegram_message_ids=[],
            sent_at=None,
        )

    def _save(self, learner_id: int, day: date, **values: Any) -> LessonRecord:
        """Insert the day's row, or overwrite an earlier failed attempt; a sent lesson is final."""
        values["schema_version"] = SCHEMA_VERSIONS["lesson"]
        with session_scope(self._sessions) as session:
            row = self._find(session, learner_id, day)
            if row is None:
                row = LessonRow(learner_id=learner_id, lesson_date=day, **values)
                session.add(row)
            elif row.status == "sent":
                raise LessonAlreadySentError(f"lesson for learner {learner_id} on {day.isoformat()} is already sent")
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            session.flush()
            return LessonRecord.model_validate(row)


class ItemRepo:
    """Learnable items; filled from A6 on."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def get_by_key(self, key: str) -> ItemRecord | None:
        with session_scope(self._sessions) as session:
            row = session.scalar(select(ItemRow).where(ItemRow.key == key))
            return None if row is None else ItemRecord.model_validate(row)

    def add(
        self,
        *,
        key: str,
        topic_id: str,
        cefr: Cefr,
        form: str,
        function_ru: str,
        explanation_ru: str,
        sources: list[Source],
        source_lesson_id: int | None = None,
    ) -> ItemRecord:
        row = ItemRow(
            key=key,
            topic_id=topic_id,
            cefr=cefr,
            form=form,
            function_ru=function_ru,
            explanation_ru=explanation_ru,
            sources=[source.model_dump(mode="json") for source in sources],
            source_lesson_id=source_lesson_id,
        )
        with session_scope(self._sessions) as session:
            session.add(row)
            session.flush()
            return ItemRecord.model_validate(row)


class CardRepo:
    """FSRS cards (one per learner and item); filled from A8 on."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def add(self, learner_id: int, item_id: int, *, fsrs_state: dict[str, Any], due_at: datetime) -> CardRecord:
        row = CardRow(learner_id=learner_id, item_id=item_id, fsrs_state=fsrs_state, due_at=due_at)
        with session_scope(self._sessions) as session:
            session.add(row)
            session.flush()
            return CardRecord.model_validate(row)

    def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]:
        """The learner's cards due at `now`, most overdue first."""
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(CardRow)
                .where(CardRow.learner_id == learner_id, CardRow.due_at <= now)
                .order_by(CardRow.due_at)
                .limit(limit)
            )
            return [CardRecord.model_validate(row) for row in rows]


class PracticeRepo:
    """The quiz and the translation task sent after each lesson, and the learner's answers to them."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None:
        with session_scope(self._sessions) as session:
            row = session.scalar(
                select(PracticeDeliveryRow).where(
                    PracticeDeliveryRow.lesson_id == lesson_id, PracticeDeliveryRow.kind == kind
                )
            )
            return None if row is None else PracticeDeliveryRecord.model_validate(row)

    def claim_delivery(self, lesson_id: int, kind: PracticeKind, item_count: int) -> PracticeDeliveryRecord | None:
        """Reserve the block before sending it; None when it is already claimed (never send twice)."""
        statement = (
            insert(PracticeDeliveryRow)
            .values(lesson_id=lesson_id, kind=kind, item_count=item_count)
            .on_conflict_do_nothing(index_elements=["lesson_id", "kind"])
            .returning(PracticeDeliveryRow.id)
        )
        with session_scope(self._sessions) as session:
            delivery_id = session.execute(statement).scalar_one_or_none()
            if delivery_id is None:
                return None
            row = session.get(PracticeDeliveryRow, delivery_id)
            return PracticeDeliveryRecord.model_validate(row)

    def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord:
        with session_scope(self._sessions) as session:
            row = session.get_one(PracticeDeliveryRow, delivery_id)
            row.sent_at = datetime.now(UTC)
            row.telegram_message_ids = message_ids
            session.flush()
            return PracticeDeliveryRecord.model_validate(row)

    def add_poll(
        self,
        delivery_id: int,
        *,
        position: int,
        telegram_poll_id: str,
        telegram_message_id: int,
        question: QuizQuestion,
    ) -> QuizPollRecord:
        row = QuizPollRow(
            delivery_id=delivery_id,
            position=position,
            telegram_poll_id=telegram_poll_id,
            telegram_message_id=telegram_message_id,
            question=question.model_dump(mode="json"),
        )
        with session_scope(self._sessions) as session:
            session.add(row)
            session.flush()
            return QuizPollRecord.model_validate(row)

    def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        """Store one answer (a wrong one also goes to `errors`); None for an unknown poll or a repeated answer."""
        with session_scope(self._sessions) as session:
            poll = session.scalar(select(QuizPollRow).where(QuizPollRow.telegram_poll_id == telegram_poll_id))
            learner = session.scalar(select(LearnerRow).where(LearnerRow.telegram_chat_id == telegram_chat_id))
            if poll is None or learner is None:
                return None
            question = QuizQuestion.model_validate(poll.question)
            is_correct = option_ids == [question.correct_option]
            answer_id = session.execute(
                insert(QuizAnswerRow)
                .values(poll_id=poll.id, learner_id=learner.id, option_ids=option_ids, is_correct=is_correct)
                .on_conflict_do_nothing(index_elements=["poll_id", "learner_id"])
                .returning(QuizAnswerRow.id)
            ).scalar_one_or_none()
            if answer_id is None:
                return None
            if not is_correct:
                session.add(
                    ErrorRow(
                        learner_id=learner.id,
                        kind="quiz",
                        original=", ".join(question.options[i] for i in option_ids if 0 <= i < len(question.options)),
                        corrected=question.options[question.correct_option],
                        error_type=question.kind,
                        explanation_ru=question.explanation_ru,
                    )
                )

            delivery = session.get_one(PracticeDeliveryRow, poll.delivery_id, with_for_update=True)
            answered = session.execute(
                select(QuizPollRow.question, QuizAnswerRow.is_correct)
                .join(QuizAnswerRow, QuizAnswerRow.poll_id == QuizPollRow.id)
                .where(QuizPollRow.delivery_id == delivery.id, QuizAnswerRow.learner_id == learner.id)
                .order_by(QuizPollRow.position)
            ).all()
            if delivery.completed_at is not None or len(answered) < delivery.item_count:
                return QuizResult(is_correct=is_correct, completed=False)
            delivery.completed_at = datetime.now(UTC)
            return QuizResult(
                is_correct=is_correct,
                completed=True,
                results=[(QuizQuestion.model_validate(payload), correct) for payload, correct in answered],
            )

    def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None:
        """The learner's latest sent translation task if it still waits for an answer (none yet, or grading failed)."""
        with session_scope(self._sessions) as session:
            found = session.execute(
                select(PracticeDeliveryRow, LessonRow)
                .join(LessonRow, LessonRow.id == PracticeDeliveryRow.lesson_id)
                .join(LearnerRow, LearnerRow.id == LessonRow.learner_id)
                .where(
                    LearnerRow.telegram_chat_id == telegram_chat_id,
                    PracticeDeliveryRow.kind == "translation",
                    PracticeDeliveryRow.sent_at.is_not(None),
                )
                .order_by(PracticeDeliveryRow.sent_at.desc())
                .limit(1)
            ).first()
            if found is None:
                return None
            delivery, lesson = found
            answer = session.scalar(select(TranslationAnswerRow).where(TranslationAnswerRow.delivery_id == delivery.id))
            if (answer is not None and answer.status != "failed") or lesson.lesson is None:
                return None
            return OpenTranslationTask(
                learner_id=lesson.learner_id,
                delivery=PracticeDeliveryRecord.model_validate(delivery),
                lesson=Lesson.model_validate(lesson.lesson),
            )

    def start_translation_answer(
        self, delivery_id: int, learner_id: int, answer_text: str
    ) -> TranslationAnswerRecord | None:
        """Take the answer for grading; a failed earlier attempt is replaced. None if one is pending or graded."""
        with session_scope(self._sessions) as session:
            row = session.scalar(
                select(TranslationAnswerRow).where(TranslationAnswerRow.delivery_id == delivery_id).with_for_update()
            )
            if row is None:
                row = TranslationAnswerRow(
                    delivery_id=delivery_id, learner_id=learner_id, answer_text=answer_text, status="pending"
                )
                session.add(row)
            elif row.status == "failed":
                row.answer_text, row.status, row.error, row.progress_message_id = answer_text, "pending", None, None
            else:
                return None
            session.flush()
            return TranslationAnswerRecord.model_validate(row)

    def set_progress_message(self, answer_id: int, message_id: int) -> None:
        with session_scope(self._sessions) as session:
            session.get_one(TranslationAnswerRow, answer_id).progress_message_id = message_id

    def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord:
        """Save the grade, close the task and log every corrected sentence in `errors`."""
        now = datetime.now(UTC)
        with session_scope(self._sessions) as session:
            row = session.get_one(TranslationAnswerRow, answer_id)
            row.status, row.grade, row.error, row.graded_at = "graded", grade.model_dump(mode="json"), None, now
            session.get_one(PracticeDeliveryRow, row.delivery_id).completed_at = now
            session.add_all(
                ErrorRow(
                    learner_id=row.learner_id,
                    kind="text",
                    original=item.learner_text,
                    corrected=item.corrected,
                    error_type=item.error_type,
                    explanation_ru=item.explanation_ru,
                )
                for item in grade.sentences
                if item.verdict == "fixed"
            )
            session.flush()
            return TranslationAnswerRecord.model_validate(row)

    def fail_translation_answer(self, answer_id: int, reason: str) -> None:
        with session_scope(self._sessions) as session:
            row = session.get_one(TranslationAnswerRow, answer_id)
            row.status, row.error = "failed", reason

    def dispute(self, answer_id: int) -> bool:
        """Flag a grade the learner disagrees with; False for an unknown or ungraded answer."""
        with session_scope(self._sessions) as session:
            row = session.get(TranslationAnswerRow, answer_id)
            if row is None or row.status != "graded":
                return False
            row.disputed = True
            return True
