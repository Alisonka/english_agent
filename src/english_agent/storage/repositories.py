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
from english_agent.models.storage import CardRecord, ItemRecord, LearnerRecord, LessonRecord
from english_agent.storage.db import SessionFactory, make_engine, make_session_factory, session_scope
from english_agent.storage.orm import CardRow, ItemRow, LearnerRow, LessonRow
from english_agent.versions import SCHEMA_VERSIONS


class LessonAlreadySentError(Exception):
    """A lesson for this learner and day is already recorded as sent."""


class LearnerStore(Protocol):
    def get(self, telegram_chat_id: str) -> LearnerRecord | None: ...

    def get_or_create(self, telegram_chat_id: str, default_cefr: Cefr) -> LearnerRecord: ...

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord: ...


class LessonStore(Protocol):
    def sent_on(self, learner_id: int, day: date) -> bool: ...

    def record_sent(
        self, learner_id: int, day: date, lesson: Lesson, message_ids: list[int], *, curriculum_version: int | None
    ) -> LessonRecord: ...

    def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord: ...


@dataclass(frozen=True)
class Stores:
    learners: LearnerStore
    lessons: LessonStore


def sql_stores(sessions: SessionFactory) -> Stores:
    return Stores(learners=LearnerRepo(sessions), lessons=LessonRepo(sessions))


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
