"""SQLAlchemy table mappings. Internal to `english_agent.storage`; callers get `models.storage` records.

Keep in sync with the Alembic migrations in `migrations/versions/`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class LearnerRow(Base):
    __tablename__ = "learners"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
    cefr: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class LessonRow(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        # One lesson per learner per day: a restart or a manual run must not send it twice.
        UniqueConstraint("learner_id", "lesson_date"),
        CheckConstraint("status IN ('sent', 'failed')", name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    lesson_date: Mapped[date] = mapped_column(Date)
    topic_id: Mapped[str] = mapped_column(String(100))
    cefr: Mapped[str] = mapped_column(String(2))
    curriculum_version: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    # `Lesson.model_dump(mode="json")`; NULL for a failed run.
    lesson: Mapped[dict[str, Any] | None] = mapped_column("payload", JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ItemRow(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    topic_id: Mapped[str] = mapped_column(String(100), index=True)
    cefr: Mapped[str] = mapped_column(String(2))
    form: Mapped[str] = mapped_column(Text)
    function_ru: Mapped[str] = mapped_column(Text)
    explanation_ru: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    source_lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created_at()


class ErrorRow(Base):
    __tablename__ = "errors"
    __table_args__ = (CheckConstraint("kind IN ('quiz', 'text')", name="kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))
    original: Mapped[str] = mapped_column(Text)
    corrected: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(64))
    explanation_ru: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class CardRow(Base):
    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("learner_id", "item_id"),
        Index("ix_cards_learner_id_due_at", "learner_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    fsrs_state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ReviewLogRow(Base):
    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    rating: Mapped[int] = mapped_column(SmallInteger)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    source: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
