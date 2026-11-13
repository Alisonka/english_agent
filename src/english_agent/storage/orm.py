"""SQLAlchemy table mappings. Internal to `english_agent.storage`; callers get `models.storage` records.

Keep in sync with the Alembic migrations in `migrations/versions/`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
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
    Time,
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
    __table_args__ = (CheckConstraint("quiz_delay_minutes >= 0", name="quiz_delay_minutes"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
    cefr: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
    # The learner's own schedule (see `models.schedule`); new learners get the settings' defaults.
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'Europe/Moscow'"))
    lesson_send_time: Mapped[time] = mapped_column(Time, server_default=text("'09:00'"))
    translation_send_time: Mapped[time] = mapped_column(Time, server_default=text("'18:00'"))
    quiz_delay_minutes: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    curriculum_started_on: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_reason: Mapped[str | None] = mapped_column(String(64))


class LessonRow(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        # One lesson per learner per day: a restart or a manual run must not send it twice.
        UniqueConstraint("learner_id", "lesson_date"),
        CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed', 'skipped')", name="status"),
        Index(
            "ix_lessons_unfinished", "lesson_date", postgresql_where=text("status IN ('pending', 'sending', 'failed')")
        ),
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
    content_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_contents.id", ondelete="RESTRICT"), index=True)
    # Who is delivering it: set when a worker claims the day, cleared when it lets go; see `LessonRepo.claim`.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class LessonContentRow(Base):
    """A lesson written once and sent to every learner of the topic and level (see `models.storage`)."""

    __tablename__ = "lesson_contents"
    __table_args__ = (
        # One piece of content per recipe: the claim that prevents two workers writing it at once.
        UniqueConstraint("topic_id", "cefr", "curriculum_version", "content_key"),
        CheckConstraint("status IN ('generating', 'ready', 'failed')", name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(100))
    cefr: Mapped[str] = mapped_column(String(2))
    curriculum_version: Mapped[int] = mapped_column(Integer)
    content_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[int] = mapped_column(Integer)
    lesson: Mapped[dict[str, Any] | None] = mapped_column("payload", JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


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
    __table_args__ = (
        CheckConstraint("kind IN ('quiz', 'text')", name="kind"),
        # The profile and the planner read "this learner's errors of the last N days".
        Index("ix_errors_learner_id_created_at", "learner_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
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
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviews: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()


class ReviewLogRow(Base):
    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    rating: Mapped[int] = mapped_column(SmallInteger)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    source: Mapped[str] = mapped_column(String(32))
    # The rated event ("quiz_answer:<id>", "translation_answer:<id>:<n>"): unique, so replaying it is a no-op.
    source_ref: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class LearnerProfileRow(Base):
    """Append-only versions of `models.profile.LearnerProfile`; the latest version is the profile."""

    __tablename__ = "learner_profiles"
    __table_args__ = (UniqueConstraint("learner_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    profile: Mapped[dict[str, Any]] = mapped_column("payload", JSONB)
    created_at: Mapped[datetime] = _created_at()


class PracticeDeliveryRow(Base):
    """One practice block (the quiz or the translation task) sent for a lesson."""

    __tablename__ = "practice_deliveries"
    __table_args__ = (
        # Claimed before sending: a second tick (or a restart) must not send the same block twice.
        UniqueConstraint("lesson_id", "kind"),
        CheckConstraint("kind IN ('quiz', 'translation')", name="kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    item_count: Mapped[int] = mapped_column(Integer)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuizPollRow(Base):
    __tablename__ = "quiz_polls"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("practice_deliveries.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    telegram_poll_id: Mapped[str] = mapped_column(String(64), unique=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    # `QuizQuestion.model_dump(mode="json")` with the options in the order they were sent.
    question: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class QuizAnswerRow(Base):
    __tablename__ = "quiz_answers"
    __table_args__ = (UniqueConstraint("poll_id", "learner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("quiz_polls.id", ondelete="CASCADE"))
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    option_ids: Mapped[list[int]] = mapped_column(JSONB)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


class TranslationAnswerRow(Base):
    __tablename__ = "translation_answers"
    __table_args__ = (
        # One graded attempt per task; a failed grading is retried on the same row.
        UniqueConstraint("delivery_id"),
        CheckConstraint("status IN ('pending', 'graded', 'failed')", name="status"),
        Index("ix_translation_answers_pending", "created_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("practice_deliveries.id", ondelete="CASCADE"))
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    answer_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    # `TranslationGrade.model_dump(mode="json")`; NULL until graded.
    grade: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    progress_message_id: Mapped[int | None] = mapped_column(Integer)
    disputed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    grading_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    grading_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
