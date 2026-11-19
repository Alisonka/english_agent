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
    Float,
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
    __table_args__ = (
        CheckConstraint("quiz_delay_minutes >= 0", name="quiz_delay_minutes"),
        CheckConstraint("goal IN ('work', 'travel', 'exam', 'general')", name="goal"),
    )

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
    # /goal and /interests: what the course leans towards and the themes of personal exercises.
    goal: Mapped[str | None] = mapped_column(String(16))
    interests: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))


class LessonRow(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        # One lesson per learner per day: a restart or a manual run must not send it twice.
        UniqueConstraint("learner_id", "lesson_date"),
        CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed', 'skipped')", name="status"),
        CheckConstraint("variant IN ('main', 'alt')", name="variant"),
        CheckConstraint("payload_kind IN ('lesson', 'step')", name="payload_kind"),
        Index(
            "ix_lessons_unfinished", "lesson_date", postgresql_where=text("status IN ('pending', 'sending', 'failed')")
        ),
        Index("ix_lessons_learner_id_topic_id", "learner_id", "topic_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    lesson_date: Mapped[date] = mapped_column(Date)
    topic_id: Mapped[str] = mapped_column(String(100))
    cefr: Mapped[str] = mapped_column(String(2))
    curriculum_version: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    # A `Lesson` or a `LessonStep` dump (see `payload_kind`); NULL for a failed run.
    payload: Mapped[dict[str, Any] | None] = mapped_column("payload", JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_contents.id", ondelete="RESTRICT"), index=True)
    # Who is delivering it: set when a worker claims the day, cleared when it lets go; see `LessonRepo.claim`.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Which step of the topic this day is, and what the payload holds. Legacy rows are step 1 of 1,
    # slug "core", a main variant whose payload is a whole `Lesson`; step rows carry a `LessonStep`.
    step_index: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    step_count: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    step_slug: Mapped[str] = mapped_column(String(40), default="core", server_default=text("'core'"))
    variant: Mapped[str] = mapped_column(String(8), default="main", server_default=text("'main'"))
    payload_kind: Mapped[str] = mapped_column(String(16), default="lesson", server_default=text("'lesson'"))


class LessonContentRow(Base):
    """A lesson written once and sent to every learner of the topic and level (see `models.storage`)."""

    __tablename__ = "lesson_contents"
    __table_args__ = (
        # One piece of content per recipe: the claim that prevents two workers writing it at once.
        UniqueConstraint("topic_id", "cefr", "curriculum_version", "content_key"),
        CheckConstraint("status IN ('generating', 'ready', 'failed')", name="status"),
        CheckConstraint("payload_kind IN ('lesson', 'topic')", name="payload_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(100))
    cefr: Mapped[str] = mapped_column(String(2))
    curriculum_version: Mapped[int] = mapped_column(Integer)
    content_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[int] = mapped_column(Integer)
    # "lesson" holds a `Lesson`; "topic" holds a whole `TopicContent` (plan + steps + cheat sheet).
    payload_kind: Mapped[str] = mapped_column(String(16), default="lesson", server_default=text("'lesson'"))
    payload: Mapped[dict[str, Any] | None] = mapped_column("payload", JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    # `RunReport.model_dump(mode="json")` of the last generation run (ready or failed); NULL before reports.
    run: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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
        CheckConstraint("kind IN ('quiz', 'translation', 'summary')", name="kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    item_count: Mapped[int] = mapped_column(Integer)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The assembled practice set for the day (per-item question/sentence tags); NULL for legacy rows.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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


EVIDENCE_KINDS = (
    "quiz_answer",
    "translation_sentence",
    "exam_answer",
    "probe",
    "tutor_question",
    "missed_day",
    "dispute",
)


class EvidenceRow(Base):
    """One event that tells something about the learner; every agent decision cites these ids."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(kind) for kind in EVIDENCE_KINDS)})", name="kind"),
        # Agents read "this learner's evidence since the last session", optionally for one topic.
        Index("ix_evidence_learner_id_occurred_at", "learner_id", "occurred_at"),
        Index("ix_evidence_learner_id_topic_id", "learner_id", "topic_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    # The event it records ("quiz_answer:<id>", "translation_answer:<id>:<n>"): unique, so a replay is a no-op.
    source_ref: Mapped[str] = mapped_column(String(64), unique=True)
    topic_id: Mapped[str | None] = mapped_column(String(100))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    error_id: Mapped[int | None] = mapped_column(ForeignKey("errors.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set when the event no longer counts (an overturned grade); cancelled evidence cannot be cited.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class JournalEntryRow(Base):
    """The Mentor's journal (`models.mentor.JournalEntry`): append-only, outcomes filled in by code."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint("outcome IN ('met', 'not_met', 'insufficient')", name="outcome"),
        Index("ix_journal_entries_learner_id_created_at", "learner_id", "created_at"),
        # The checker reads "expectations due and not checked yet".
        Index(
            "ix_journal_entries_unchecked", "due_on", postgresql_where=text("outcome IS NULL AND due_on IS NOT NULL")
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[int]] = mapped_column(JSONB)
    # `Expectation.model_dump(mode="json")`; NULL for a decision that expects nothing checkable.
    expectation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    due_on: Mapped[date | None] = mapped_column(Date)  # copied from the expectation, for the index
    outcome: Mapped[str | None] = mapped_column(String(16))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class HypothesisRow(Base):
    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'probing', 'confirmed', 'refuted', 'stale')", name="status"),
        Index("ix_hypotheses_learner_id_status", "learner_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    statement_ru: Mapped[str] = mapped_column(Text)
    topic_id: Mapped[str | None] = mapped_column(String(100))
    item_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    evidence_ids: Mapped[list[int]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_evidence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DossierRow(Base):
    """Append-only versions of `models.mentor.Dossier`; the newest is the learner's dossier."""

    __tablename__ = "dossiers"
    __table_args__ = (Index("ix_dossiers_learner_id_created_at", "learner_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    until_entry_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class LearnerAbilityRow(Base):
    """The learner's ability (`knowledge.py`): one overall rating and one offset per tag group."""

    __tablename__ = "learner_abilities"

    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)  # "overall" or a topic tag
    rating: Mapped[float] = mapped_column(Float)
    answers: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class QuestionStatRow(Base):
    """A shared bank question's difficulty, calibrated on every learner's answers."""

    __tablename__ = "question_stats"
    __table_args__ = (Index("ix_question_stats_item_id", "item_id"),)

    question_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    topic_id: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))
    difficulty: Mapped[float] = mapped_column(Float)
    prior: Mapped[float] = mapped_column(Float)  # the starting estimate, before any answer
    prior_source: Mapped[str] = mapped_column(String(16))  # "level" (topic level + kind) or "critic"
    answers: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class TopicMarkRow(Base):
    """A topic credited by the placement test or confirmed by an exam, for one learner."""

    __tablename__ = "topic_marks"
    __table_args__ = (CheckConstraint("mark IN ('credited', 'confirmed')", name="mark"),)

    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    mark: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_ref: Mapped[str] = mapped_column(String(64))  # the test or exam that set it
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DayPlanRow(Base):
    """One learner's plan for one day (`models.plan.DayPlan`); committed once, then only executed."""

    __tablename__ = "day_plans"
    __table_args__ = (
        UniqueConstraint("learner_id", "plan_date"),
        CheckConstraint("state IN ('pending', 'running', 'committed', 'fallback')", name="state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    plan_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(16))
    # `DayPlan.model_dump(mode="json")`; NULL while a Mentor session is still running.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The Mentor's session trace: tool calls, results, policy refusals. NULL for a fallback plan.
    trajectory: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContentOrderRow(Base):
    """A remedial or contrast lesson the Mentor ordered (`models.content_order`); shared per brief."""

    __tablename__ = "content_orders"
    __table_args__ = (
        UniqueConstraint("topic_id", "cefr"),
        CheckConstraint("status IN ('ordered', 'ready', 'failed')", name="status"),
        Index("ix_content_orders_open", "created_at", postgresql_where=text("status = 'ordered'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(100))  # the brief's synthetic topic id
    cefr: Mapped[str] = mapped_column(String(2))
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    content_id: Mapped[int | None] = mapped_column(ForeignKey("lesson_contents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
