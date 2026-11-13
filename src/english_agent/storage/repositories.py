"""Repositories: the only code that touches ORM rows. They take and return pydantic records.

Every method is a coroutine that runs in its own short transaction (see `session_scope`), so
one set of repositories serves every concurrent handler and job of the process. Rows are
loaded completely inside the transaction (no lazy loading), which async SQLAlchemy requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import cast, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from english_agent.config import Settings
from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.profile import LearnerProfile
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import (
    CORE_SLUG,
    CardRecord,
    ClaimOutcome,
    ErrorRecord,
    GradingTask,
    ItemDraft,
    ItemRecord,
    LearnerProfileRecord,
    LearnerRecord,
    LessonClaim,
    LessonContentRecord,
    LessonRecord,
    LessonState,
    LessonStatus,
    OpenTranslationTask,
    PracticeDeliveryRecord,
    PracticeKind,
    ProfileInputs,
    QuizPollRecord,
    QuizResult,
    TranslationAnswerRecord,
    core_item_key,
)
from english_agent.srs import ReviewScheduler, due_at, make_scheduler, quiz_rating, translation_rating
from english_agent.storage.db import SessionFactory, make_engine, make_session_factory, session_scope
from english_agent.storage.orm import (
    CardRow,
    ErrorRow,
    ItemRow,
    LearnerProfileRow,
    LearnerRow,
    LessonContentRow,
    LessonRow,
    PracticeDeliveryRow,
    QuizAnswerRow,
    QuizPollRow,
    ReviewLogRow,
    TranslationAnswerRow,
)
from english_agent.versions import SCHEMA_VERSIONS


class LessonAlreadySentError(Exception):
    """A lesson for this learner and day is already recorded as sent."""


class LearnerStore(Protocol):
    async def get(self, telegram_chat_id: str) -> LearnerRecord | None: ...

    async def get_by_id(self, learner_id: int) -> LearnerRecord | None: ...

    async def get_or_create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord: ...

    async def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord: ...

    async def list_active(self) -> list[LearnerRecord]: ...

    async def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None: ...

    async def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None: ...

    async def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None: ...

    async def deactivate(self, telegram_chat_id: str, reason: str) -> None: ...


class LessonStore(Protocol):
    async def get(self, learner_id: int, day: date) -> LessonRecord | None: ...

    async def sent_on(self, learner_id: int, day: date) -> bool: ...

    async def record_sent(
        self,
        learner_id: int,
        day: date,
        lesson: Lesson,
        message_ids: list[int],
        *,
        curriculum_version: int | None,
        content_id: int | None = None,
    ) -> LessonRecord: ...

    async def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord: ...

    async def list_sent(self) -> list[LessonRecord]: ...

    async def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]: ...

    async def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]: ...

    async def states(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonState]: ...

    async def claim(
        self,
        learner_id: int,
        day: date,
        *,
        topic_id: str,
        cefr: Cefr,
        curriculum_version: int,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
    ) -> tuple[LessonClaim, LessonRecord]: ...

    async def release(self, lesson_id: int) -> None: ...

    async def start_sending(self, lesson_id: int, content_id: int, lesson: Lesson) -> LessonRecord: ...

    async def add_message_id(self, lesson_id: int, message_id: int) -> None: ...

    async def finish_sending(self, lesson_id: int) -> LessonRecord: ...

    async def fail(self, lesson_id: int, reason: str, *, final: bool) -> LessonRecord: ...

    async def close_past(self, before: date) -> int: ...


class LessonContentStore(Protocol):
    async def claim(
        self,
        topic_id: str,
        cefr: Cefr,
        curriculum_version: int,
        content_key: str,
        *,
        owner: str,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
    ) -> tuple[ClaimOutcome, LessonContentRecord]: ...

    async def finish(self, content_id: int, lesson: Lesson) -> LessonContentRecord: ...

    async def fail(self, content_id: int, reason: str) -> LessonContentRecord: ...

    async def unavailable_topics(
        self, cefr: Cefr, curriculum_version: int, content_key: str, *, max_attempts: int
    ) -> set[str]: ...


class ItemStore(Protocol):
    async def get_by_key(self, key: str) -> ItemRecord | None: ...

    async def get_or_create(
        self, draft: ItemDraft, *, source_lesson_id: int | None = None
    ) -> tuple[ItemRecord, bool]: ...


class PracticeStore(Protocol):
    async def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None: ...

    async def claim_delivery(
        self, lesson_id: int, kind: PracticeKind, item_count: int
    ) -> PracticeDeliveryRecord | None: ...

    async def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord: ...

    async def add_poll(
        self,
        delivery_id: int,
        *,
        position: int,
        telegram_poll_id: str,
        telegram_message_id: int,
        item_id: int | None,
        question: QuizQuestion,
    ) -> QuizPollRecord: ...

    async def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None: ...

    async def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None: ...

    async def start_translation_answer(
        self, delivery_id: int, learner_id: int, answer_text: str
    ) -> TranslationAnswerRecord | None: ...

    async def set_progress_message(self, answer_id: int, message_id: int) -> None: ...

    async def translation_for_grading(self, answer_id: int) -> GradingTask | None: ...

    async def begin_grading(self, answer_id: int, now: datetime) -> GradingTask | None: ...

    async def stale_gradings(self, started_before: datetime, *, limit: int = 200) -> list[tuple[int, int]]: ...

    async def delivered_kinds(self, lesson_ids: list[int]) -> dict[int, set[PracticeKind]]: ...

    async def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord: ...

    async def fail_translation_answer(self, answer_id: int, reason: str) -> None: ...

    async def dispute(self, answer_id: int) -> bool: ...


class CardStore(Protocol):
    async def get(self, learner_id: int, item_id: int) -> CardRecord | None: ...

    async def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]: ...

    async def review(
        self,
        learner_id: int,
        item_id: int,
        rating: int,
        at: datetime,
        *,
        source: str,
        source_ref: str,
        payload: dict[str, Any] | None = None,
    ) -> CardRecord: ...


class ProfileStore(Protocol):
    async def latest(self, learner_id: int) -> LearnerProfileRecord | None: ...

    async def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord: ...

    async def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs: ...


@dataclass(frozen=True)
class Stores:
    learners: LearnerStore
    lessons: LessonStore
    practice: PracticeStore
    items: ItemStore
    cards: CardStore
    profiles: ProfileStore
    contents: LessonContentStore


def sql_stores(sessions: SessionFactory, scheduler: ReviewScheduler | None = None) -> Stores:
    scheduler = scheduler or ReviewScheduler()
    return Stores(
        learners=LearnerRepo(sessions),
        lessons=LessonRepo(sessions),
        practice=PracticeRepo(sessions, scheduler),
        items=ItemRepo(sessions),
        cards=CardRepo(sessions, scheduler),
        profiles=ProfileRepo(sessions),
        contents=LessonContentRepo(sessions),
    )


@asynccontextmanager
async def open_stores(settings: Settings) -> AsyncIterator[Stores | None]:
    """SQL-backed stores for DATABASE_URL (None when it is not set); the engine is disposed on exit."""
    if not settings.database_url.get_secret_value():
        yield None
        return
    engine = make_engine(settings)
    try:
        yield sql_stores(make_session_factory(engine), make_scheduler(settings))
    finally:
        await engine.dispose()


async def _review_card(
    session: AsyncSession,
    scheduler: ReviewScheduler,
    *,
    learner_id: int,
    item_id: int,
    rating: int,
    at: datetime,
    source: str,
    source_ref: str,
    payload: dict[str, Any] | None = None,
) -> tuple[CardRow, bool]:
    """Rate one item once, inside the caller's transaction.

    Creates the card on first contact, applies FSRS and writes the review log. The flag is False
    when `source_ref` was rated before: the card is then returned unchanged (safe replays).
    """
    locked = select(CardRow).where(CardRow.learner_id == learner_id, CardRow.item_id == item_id).with_for_update()
    card = await session.scalar(locked)
    if card is None:
        # ON CONFLICT: the polling and the grading thread may rate the same new item at once.
        await session.execute(
            insert(CardRow)
            .values(learner_id=learner_id, item_id=item_id, fsrs_state=scheduler.new_card(at), due_at=at)
            .on_conflict_do_nothing(index_elements=["learner_id", "item_id"])
        )
        card = await session.scalar(locked)
        assert card is not None
    # Checked under the card lock, so two threads with the same event cannot both apply it.
    if await session.scalar(select(ReviewLogRow.id).where(ReviewLogRow.source_ref == source_ref)) is not None:
        return card, False
    state, log = scheduler.review(card.fsrs_state, rating, at)
    card.fsrs_state, card.due_at = state, due_at(state)
    card.last_reviewed_at, card.reviews = at, card.reviews + 1
    session.add(
        ReviewLogRow(
            card_id=card.id,
            rating=rating,
            reviewed_at=at,
            source=source,
            source_ref=source_ref,
            payload={"fsrs": log, **(payload or {})},
        )
    )
    await session.flush()
    return card, True


async def _core_item(session: AsyncSession, topic_id: str) -> ItemRow | None:
    return await session.scalar(select(ItemRow).where(ItemRow.key == core_item_key(topic_id)))


class LearnerRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    async def _find(session: AsyncSession, telegram_chat_id: str) -> LearnerRow | None:
        return await session.scalar(select(LearnerRow).where(LearnerRow.telegram_chat_id == telegram_chat_id))

    async def get(self, telegram_chat_id: str) -> LearnerRecord | None:
        async with session_scope(self._sessions) as session:
            row = await self._find(session, telegram_chat_id)
            return None if row is None else LearnerRecord.model_validate(row)

    async def get_by_id(self, learner_id: int) -> LearnerRecord | None:
        async with session_scope(self._sessions) as session:
            row = await session.get(LearnerRow, learner_id)
            return None if row is None else LearnerRecord.model_validate(row)

    async def get_or_create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord:
        """The learner, created with `default_cefr` and `schedule` (else the column defaults) if new."""
        values: dict[str, Any] = {"telegram_chat_id": telegram_chat_id, "cefr": default_cefr}
        if schedule is not None:
            values.update(schedule.model_dump())
        # ON CONFLICT: the polling and scheduler threads may both create the same learner.
        statement = (
            insert(LearnerRow).values(**values).on_conflict_do_nothing(index_elements=[LearnerRow.telegram_chat_id])
        )
        async with session_scope(self._sessions) as session:
            await session.execute(statement)
            row = await self._find(session, telegram_chat_id)
            assert row is not None
            return LearnerRecord.model_validate(row)

    async def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord:
        """Set the learner's level, creating the learner if needed."""
        statement = insert(LearnerRow).values(telegram_chat_id=telegram_chat_id, cefr=cefr)
        statement = statement.on_conflict_do_update(
            index_elements=[LearnerRow.telegram_chat_id],
            set_={"cefr": statement.excluded.cefr, "updated_at": func.now()},
        )
        async with session_scope(self._sessions) as session:
            await session.execute(statement)
            row = await self._find(session, telegram_chat_id)
            assert row is not None
            return LearnerRecord.model_validate(row)

    async def list_active(self) -> list[LearnerRecord]:
        """Learners who may get lessons: started, not paused, not deactivated. Oldest first."""
        async with session_scope(self._sessions) as session:
            rows = await session.scalars(
                select(LearnerRow)
                .where(
                    LearnerRow.is_active.is_(True),
                    LearnerRow.paused_at.is_(None),
                    LearnerRow.curriculum_started_on.is_not(None),
                )
                .order_by(LearnerRow.id)
            )
            return [LearnerRecord.model_validate(row) for row in rows]

    async def _change(self, telegram_chat_id: str, **values: Any) -> LearnerRecord | None:
        statement = (
            update(LearnerRow)
            .where(LearnerRow.telegram_chat_id == telegram_chat_id)
            .values(**values, updated_at=func.now())
            .returning(LearnerRow)
        )
        async with session_scope(self._sessions) as session:
            row = await session.scalar(statement)
            return None if row is None else LearnerRecord.model_validate(row)

    async def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None:
        """Replace the learner's schedule; None for an unknown chat."""
        return await self._change(telegram_chat_id, **schedule.model_dump())

    async def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None:
        """/start: (re)activate the learner; the course start is set once and then kept."""
        return await self._change(
            telegram_chat_id,
            curriculum_started_on=func.coalesce(LearnerRow.curriculum_started_on, on),
            is_active=True,
            deactivated_reason=None,
        )

    async def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None:
        # COALESCE: pausing twice keeps the original pause time.
        paused_at = func.coalesce(LearnerRow.paused_at, func.now()) if paused else None
        return await self._change(telegram_chat_id, paused_at=paused_at)

    async def deactivate(self, telegram_chat_id: str, reason: str) -> None:
        """Stop all sending to the chat (the bot was blocked, the course is over) until /start."""
        await self._change(telegram_chat_id, is_active=False, deactivated_reason=reason)


class LessonRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    async def _find(session: AsyncSession, learner_id: int, day: date) -> LessonRow | None:
        return await session.scalar(
            select(LessonRow).where(LessonRow.learner_id == learner_id, LessonRow.lesson_date == day)
        )

    async def get(self, learner_id: int, day: date) -> LessonRecord | None:
        async with session_scope(self._sessions) as session:
            row = await self._find(session, learner_id, day)
            return None if row is None else LessonRecord.model_validate(row)

    async def sent_on(self, learner_id: int, day: date) -> bool:
        async with session_scope(self._sessions) as session:
            row = await self._find(session, learner_id, day)
            return row is not None and row.status == "sent"

    async def record_sent(
        self,
        learner_id: int,
        day: date,
        lesson: Lesson,
        message_ids: list[int],
        *,
        curriculum_version: int | None,
        content_id: int | None = None,
    ) -> LessonRecord:
        # The payload is still written next to content_id, so code that predates shared content
        # (and a rollback to it) keeps reading lessons.payload.
        return await self._save(
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
            content_id=content_id,
        )

    async def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord:
        return await self._save(
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

    async def list_sent(self) -> list[LessonRecord]:
        """Every sent lesson, oldest first. For maintenance commands: one lesson a day, so no paging."""
        async with session_scope(self._sessions) as session:
            rows = await session.scalars(
                select(LessonRow).where(LessonRow.status == "sent").order_by(LessonRow.lesson_date, LessonRow.id)
            )
            return [LessonRecord.model_validate(row) for row in rows]

    async def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]:
        """The status of each (learner id, day) that has a row; one query, no payloads."""
        if not keys:
            return {}
        async with session_scope(self._sessions) as session:
            rows = await session.execute(
                select(LessonRow.learner_id, LessonRow.lesson_date, LessonRow.status).where(
                    tuple_(LessonRow.learner_id, LessonRow.lesson_date).in_(keys)
                )
            )
            return {(learner_id, day): status for learner_id, day, status in rows}

    async def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]:
        """Topics of this level the learner already got: the next lesson is the first topic not in here."""
        async with session_scope(self._sessions) as session:
            rows = await session.scalars(
                select(LessonRow.topic_id).where(
                    LessonRow.learner_id == learner_id, LessonRow.cefr == cefr, LessonRow.status == "sent"
                )
            )
            return set(rows)

    async def states(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonState]:
        """The rows of these (learner id, day) keys without their payload, one query."""
        if not keys:
            return {}
        async with session_scope(self._sessions) as session:
            rows = await session.execute(
                select(
                    LessonRow.learner_id,
                    LessonRow.lesson_date,
                    LessonRow.id,
                    LessonRow.status,
                    LessonRow.attempts,
                    LessonRow.claimed_at,
                    LessonRow.sent_at,
                ).where(tuple_(LessonRow.learner_id, LessonRow.lesson_date).in_(keys))
            )
            return {
                (learner_id, day): LessonState(
                    lesson_id=lesson_id, status=status, attempts=attempts, claimed_at=claimed_at, sent_at=sent_at
                )
                for learner_id, day, lesson_id, status, attempts, claimed_at, sent_at in rows
            }

    async def claim(
        self,
        learner_id: int,
        day: date,
        *,
        topic_id: str,
        cefr: Cefr,
        curriculum_version: int,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
    ) -> tuple[LessonClaim, LessonRecord]:
        """Take the day's delivery, in one transaction.

        - "claimed": the caller delivers it now (a new day, a released one, a stale "pending", or a
          "failed" one with attempts left);
        - "resume": a worker died while sending: the caller sends the rest (the ids of the messages
          already sent are in `telegram_message_ids`) and keeps the row's topic;
        - "busy": another worker holds it right now;
        - "done": sent, skipped, or out of attempts.
        """
        new = (
            insert(LessonRow)
            .values(
                learner_id=learner_id,
                lesson_date=day,
                topic_id=topic_id,
                cefr=cefr,
                curriculum_version=curriculum_version,
                schema_version=SCHEMA_VERSIONS["lesson"],
                status="pending",
                attempts=1,
                claimed_at=now,
            )
            .on_conflict_do_nothing(index_elements=["learner_id", "lesson_date"])
            .returning(LessonRow.id)
        )
        async with session_scope(self._sessions) as session:
            created_id = (await session.execute(new)).scalar_one_or_none()
            row = await session.scalar(
                select(LessonRow)
                .where(LessonRow.learner_id == learner_id, LessonRow.lesson_date == day)
                .with_for_update()
            )
            assert row is not None
            if created_id is not None:
                return "claimed", LessonRecord.model_validate(row)
            fresh = row.claimed_at is not None and row.claimed_at > now - stale_after
            outcome: LessonClaim
            if row.status in ("sent", "skipped"):
                outcome = "done"
            elif row.status in ("pending", "sending") and fresh:
                outcome = "busy"
            elif row.attempts >= max_attempts:
                row.status, row.error = "skipped", row.error or f"gave up after {row.attempts} attempts"
                outcome = "done"
            else:
                # Every claim counts as an attempt; `release` takes it back when nothing was tried.
                outcome = "resume" if row.status == "sending" else "claimed"
                if outcome == "claimed":
                    row.topic_id, row.cefr, row.curriculum_version = topic_id, cefr, curriculum_version
                    row.status = "pending"
                row.attempts, row.claimed_at = row.attempts + 1, now
            await session.flush()
            return outcome, LessonRecord.model_validate(row)

    async def release(self, lesson_id: int) -> None:
        """Give the day back without counting an attempt (the content is still being written elsewhere)."""
        async with session_scope(self._sessions) as session:
            await session.execute(
                update(LessonRow)
                .where(LessonRow.id == lesson_id, LessonRow.status == "pending")
                .values(claimed_at=None, attempts=func.greatest(LessonRow.attempts - 1, 0))
            )

    async def start_sending(self, lesson_id: int, content_id: int, lesson: Lesson) -> LessonRecord:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(LessonRow, lesson_id, with_for_update=True)
            row.status, row.content_id, row.error = "sending", content_id, None
            # The payload is still copied next to content_id: practice and older code read it from here.
            row.lesson, row.schema_version = lesson.model_dump(mode="json"), SCHEMA_VERSIONS["lesson"]
            row.topic_id, row.cefr = lesson.topic_id, lesson.cefr
            await session.flush()
            return LessonRecord.model_validate(row)

    async def add_message_id(self, lesson_id: int, message_id: int) -> None:
        """Record one sent chunk right away, so a crash mid-lesson resumes after it instead of repeating it."""
        appended = LessonRow.telegram_message_ids.op("||")(cast([message_id], JSONB))
        async with session_scope(self._sessions) as session:
            await session.execute(
                update(LessonRow).where(LessonRow.id == lesson_id).values(telegram_message_ids=appended)
            )

    async def finish_sending(self, lesson_id: int) -> LessonRecord:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(LessonRow, lesson_id, with_for_update=True)
            row.status, row.sent_at, row.claimed_at = "sent", datetime.now(UTC), None
            await session.flush()
            return LessonRecord.model_validate(row)

    async def fail(self, lesson_id: int, reason: str, *, final: bool) -> LessonRecord:
        """A failed attempt: "failed" is retried later by the planner, a final one is "skipped" for the day."""
        async with session_scope(self._sessions) as session:
            row = await session.get_one(LessonRow, lesson_id, with_for_update=True)
            if row.status != "sent":
                # claimed_at stays: the planner waits a backoff after the last attempt before the next one.
                row.status, row.error = ("skipped" if final else "failed"), reason
            await session.flush()
            return LessonRecord.model_validate(row)

    async def close_past(self, before: date) -> int:
        """Mark unfinished rows of days before `before` as skipped; returns how many."""
        async with session_scope(self._sessions) as session:
            result = await session.execute(
                update(LessonRow)
                .where(LessonRow.lesson_date < before, LessonRow.status.in_(("pending", "sending", "failed")))
                .values(status="skipped", claimed_at=None, error=func.coalesce(LessonRow.error, "not sent that day"))
            )
            return int(result.rowcount or 0)

    async def _save(self, learner_id: int, day: date, **values: Any) -> LessonRecord:
        """Insert the day's row, or overwrite an earlier failed attempt; a sent lesson is final."""
        values["schema_version"] = SCHEMA_VERSIONS["lesson"]
        async with session_scope(self._sessions) as session:
            row = await self._find(session, learner_id, day)
            if row is None:
                row = LessonRow(learner_id=learner_id, lesson_date=day, **values)
                session.add(row)
            elif row.status == "sent":
                raise LessonAlreadySentError(f"lesson for learner {learner_id} on {day.isoformat()} is already sent")
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            await session.flush()
            return LessonRecord.model_validate(row)


class LessonContentRepo:
    """Shared lesson content and the claim that makes sure one worker at a time writes it."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def claim(
        self,
        topic_id: str,
        cefr: Cefr,
        curriculum_version: int,
        content_key: str,
        *,
        owner: str,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
    ) -> tuple[ClaimOutcome, LessonContentRecord]:
        """Get the content, or the right to write it, in one transaction.

        - "ready": written already, send it;
        - "claimed": the caller writes it now and then calls `finish` or `fail`;
        - "busy": someone else is writing it, or its last attempt failed less than `stale_after`
          ago (a cool-down, so an outage does not burn every attempt in a minute): try later;
        - "exhausted": `max_attempts` attempts failed; the topic is skipped.

        A "generating" claim older than `stale_after` belongs to a dead worker and is taken over.
        """
        new = (
            insert(LessonContentRow)
            .values(
                topic_id=topic_id,
                cefr=cefr,
                curriculum_version=curriculum_version,
                content_key=content_key,
                status="generating",
                schema_version=SCHEMA_VERSIONS["lesson"],
                attempts=1,
                claimed_at=now,
                claimed_by=owner,
            )
            .on_conflict_do_nothing(index_elements=["topic_id", "cefr", "curriculum_version", "content_key"])
            .returning(LessonContentRow.id)
        )
        async with session_scope(self._sessions) as session:
            created_id = (await session.execute(new)).scalar_one_or_none()
            row = await session.scalar(
                select(LessonContentRow)
                .where(
                    LessonContentRow.topic_id == topic_id,
                    LessonContentRow.cefr == cefr,
                    LessonContentRow.curriculum_version == curriculum_version,
                    LessonContentRow.content_key == content_key,
                )
                .with_for_update()
            )
            assert row is not None
            outcome = self._decide(row, created=created_id is not None, now=now, stale_after=stale_after)
            if outcome == "claimed" and created_id is None:
                if row.attempts >= max_attempts:
                    row.status, row.error = "failed", row.error or "abandoned on its last attempt"
                    outcome = "exhausted"
                else:
                    row.status, row.error = "generating", None
                    row.attempts, row.claimed_at, row.claimed_by = row.attempts + 1, now, owner
            elif outcome == "busy" and row.status == "failed" and row.attempts >= max_attempts:
                outcome = "exhausted"
            await session.flush()
            return outcome, LessonContentRecord.model_validate(row)

    @staticmethod
    def _decide(row: LessonContentRow, *, created: bool, now: datetime, stale_after: timedelta) -> ClaimOutcome:
        if created:
            return "claimed"
        if row.status == "ready":
            return "ready"
        if row.claimed_at is not None and row.claimed_at > now - stale_after:
            return "busy"
        return "claimed"

    async def finish(self, content_id: int, lesson: Lesson) -> LessonContentRecord:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(LessonContentRow, content_id, with_for_update=True)
            row.status, row.lesson, row.error = "ready", lesson.model_dump(mode="json"), None
            row.schema_version, row.generated_at = SCHEMA_VERSIONS["lesson"], datetime.now(UTC)
            await session.flush()
            return LessonContentRecord.model_validate(row)

    async def fail(self, content_id: int, reason: str) -> LessonContentRecord:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(LessonContentRow, content_id, with_for_update=True)
            if row.status != "ready":  # a late failure never overwrites content another worker finished
                row.status, row.error = "failed", reason
            await session.flush()
            return LessonContentRecord.model_validate(row)

    async def unavailable_topics(
        self, cefr: Cefr, curriculum_version: int, content_key: str, *, max_attempts: int
    ) -> set[str]:
        """Topics of this level and recipe whose every attempt failed: skipped by `next_topic`."""
        async with session_scope(self._sessions) as session:
            rows = await session.scalars(
                select(LessonContentRow.topic_id).where(
                    LessonContentRow.cefr == cefr,
                    LessonContentRow.curriculum_version == curriculum_version,
                    LessonContentRow.content_key == content_key,
                    LessonContentRow.status == "failed",
                    LessonContentRow.attempts >= max_attempts,
                )
            )
            return set(rows)


class ItemRepo:
    """Learnable items: the core item of every taught topic (see `english_agent.items`)."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    async def _find(session: AsyncSession, key: str) -> ItemRow | None:
        return await session.scalar(select(ItemRow).where(ItemRow.key == key))

    async def get_by_key(self, key: str) -> ItemRecord | None:
        async with session_scope(self._sessions) as session:
            row = await self._find(session, key)
            return None if row is None else ItemRecord.model_validate(row)

    async def get_or_create(self, draft: ItemDraft, *, source_lesson_id: int | None = None) -> tuple[ItemRecord, bool]:
        """The item with `draft.key`, created from the draft if missing; the flag says whether it was created.

        An existing item is returned unchanged, so the lesson that first taught a topic stays its
        source. ON CONFLICT: the same item may be created from two threads or runs at once.
        """
        statement = (
            insert(ItemRow)
            .values(**draft.model_dump(mode="json"), source_lesson_id=source_lesson_id)
            .on_conflict_do_nothing(index_elements=[ItemRow.key])
            .returning(ItemRow.id)
        )
        async with session_scope(self._sessions) as session:
            created_id = (await session.execute(statement)).scalar_one_or_none()
            row = await self._find(session, draft.key)
            assert row is not None
            return ItemRecord.model_validate(row), created_id is not None


class CardRepo:
    """FSRS cards, one per learner and item. Practice rates them itself; this is for readers and replays."""

    def __init__(self, sessions: SessionFactory, scheduler: ReviewScheduler) -> None:
        self._sessions = sessions
        self._scheduler = scheduler

    async def get(self, learner_id: int, item_id: int) -> CardRecord | None:
        async with session_scope(self._sessions) as session:
            row = await session.scalar(
                select(CardRow).where(CardRow.learner_id == learner_id, CardRow.item_id == item_id)
            )
            return None if row is None else CardRecord.model_validate(row)

    async def review(
        self,
        learner_id: int,
        item_id: int,
        rating: int,
        at: datetime,
        *,
        source: str,
        source_ref: str,
        payload: dict[str, Any] | None = None,
    ) -> CardRecord:
        async with session_scope(self._sessions) as session:
            row, _ = await _review_card(
                session,
                self._scheduler,
                learner_id=learner_id,
                item_id=item_id,
                rating=rating,
                at=at,
                source=source,
                source_ref=source_ref,
                payload=payload,
            )
            return CardRecord.model_validate(row)

    async def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]:
        """The learner's cards due at `now`, most overdue first."""
        async with session_scope(self._sessions) as session:
            rows = await session.scalars(
                select(CardRow)
                .where(CardRow.learner_id == learner_id, CardRow.due_at <= now)
                .order_by(CardRow.due_at)
                .limit(limit)
            )
            return [CardRecord.model_validate(row) for row in rows]


class ProfileRepo:
    """Versions of the learner profile, and the raw numbers a new version is computed from."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def latest(self, learner_id: int) -> LearnerProfileRecord | None:
        async with session_scope(self._sessions) as session:
            row = await session.scalar(
                select(LearnerProfileRow)
                .where(LearnerProfileRow.learner_id == learner_id)
                .order_by(LearnerProfileRow.version.desc())
                .limit(1)
            )
            return None if row is None else LearnerProfileRecord.model_validate(row)

    async def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord:
        async with session_scope(self._sessions) as session:
            # The learner row lock serialises appends from the polling and the grading thread.
            await session.get_one(LearnerRow, learner_id, with_for_update=True)
            current = await session.scalar(
                select(func.max(LearnerProfileRow.version)).where(LearnerProfileRow.learner_id == learner_id)
            )
            row = LearnerProfileRow(
                learner_id=learner_id,
                version=(current or 0) + 1,
                schema_version=SCHEMA_VERSIONS["learner_profile"],
                profile=profile.model_dump(mode="json"),
            )
            session.add(row)
            await session.flush()
            return LearnerProfileRecord.model_validate(row)

    async def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs:
        since = now - timedelta(days=window_days)
        async with session_scope(self._sessions) as session:
            learner = await session.get_one(LearnerRow, learner_id)
            lessons_sent = await session.scalar(
                select(func.count())
                .select_from(LessonRow)
                .where(LessonRow.learner_id == learner_id, LessonRow.status == "sent")
            )
            errors = (
                await session.scalars(
                    select(ErrorRow).where(ErrorRow.learner_id == learner_id, ErrorRow.created_at >= since)
                )
            ).all()
            item_ids = {error.item_id for error in errors if error.item_id is not None}
            items = (await session.scalars(select(ItemRow).where(ItemRow.id.in_(item_ids)))).all() if item_ids else []
            quiz_answers, quiz_correct = (
                await session.execute(
                    select(func.count(), func.count().filter(QuizAnswerRow.is_correct.is_(True)))
                    .select_from(QuizAnswerRow)
                    .where(QuizAnswerRow.learner_id == learner_id, QuizAnswerRow.answered_at >= since)
                )
            ).one()
            grades = (
                await session.scalars(
                    select(TranslationAnswerRow.grade).where(
                        TranslationAnswerRow.learner_id == learner_id,
                        TranslationAnswerRow.status == "graded",
                        TranslationAnswerRow.graded_at >= since,
                    )
                )
            ).all()
            cards, due_cards = (
                await session.execute(
                    select(func.count(), func.count().filter(CardRow.due_at <= now))
                    .select_from(CardRow)
                    .where(CardRow.learner_id == learner_id)
                )
            ).one()
            return ProfileInputs(
                cefr=learner.cefr,
                window_days=window_days,
                lessons_sent=lessons_sent or 0,
                errors=[ErrorRecord.model_validate(error) for error in errors],
                items={item.id: ItemRecord.model_validate(item) for item in items},
                quiz_answers=quiz_answers,
                quiz_correct=quiz_correct,
                translation_grades=[TranslationGrade.model_validate(grade) for grade in grades if grade is not None],
                cards=cards,
                due_cards=due_cards,
            )


class MaintenanceRepo:
    """One-off repairs run from `python -m english_agent.storage`; every method is safe to rerun."""

    def __init__(self, sessions: SessionFactory, scheduler: ReviewScheduler) -> None:
        self._sessions = sessions
        self._scheduler = scheduler

    async def link_polls_to_items(self) -> int:
        """Point polls sent before items existed at their lesson's core item; returns how many were linked."""
        statement = (
            update(QuizPollRow)
            .where(
                QuizPollRow.item_id.is_(None),
                QuizPollRow.delivery_id == PracticeDeliveryRow.id,
                PracticeDeliveryRow.lesson_id == LessonRow.id,
                ItemRow.key == LessonRow.topic_id + f":{CORE_SLUG}",
            )
            .values(item_id=ItemRow.id)
        )
        async with session_scope(self._sessions) as session:
            return (await session.execute(statement)).rowcount

    async def replay_reviews(self) -> int:
        """Rate every stored quiz answer and graded translation that has no review log yet, oldest first."""
        events: list[tuple[datetime, int, int, int, str, str, dict[str, Any]]] = []
        async with session_scope(self._sessions) as session:
            answers = (
                await session.execute(
                    select(QuizAnswerRow, QuizPollRow.item_id)
                    .join(QuizPollRow, QuizPollRow.id == QuizAnswerRow.poll_id)
                    .where(QuizPollRow.item_id.is_not(None))
                )
            ).all()
            for answer, item_id in answers:
                rating = quiz_rating(answer.is_correct)
                ref = f"quiz_answer:{answer.id}"
                payload = {"poll_id": answer.poll_id}
                events.append((answer.answered_at, answer.learner_id, item_id, rating, "quiz", ref, payload))
            graded = (
                await session.execute(
                    select(TranslationAnswerRow, LessonRow.topic_id)
                    .join(PracticeDeliveryRow, PracticeDeliveryRow.id == TranslationAnswerRow.delivery_id)
                    .join(LessonRow, LessonRow.id == PracticeDeliveryRow.lesson_id)
                    .where(TranslationAnswerRow.status == "graded", TranslationAnswerRow.grade.is_not(None))
                )
            ).all()
            for answer, topic_id in graded:
                item = await _core_item(session, topic_id)
                if item is None or answer.graded_at is None:
                    continue
                for sentence in TranslationGrade.model_validate(answer.grade).sentences:
                    rating = translation_rating(sentence.verdict)
                    if rating is None:
                        continue
                    ref = f"translation_answer:{answer.id}:{sentence.n}"
                    payload = {"verdict": sentence.verdict}
                    events.append((answer.graded_at, answer.learner_id, item.id, rating, "translation", ref, payload))

            applied = 0
            for at, learner_id, item_id, rating, source, source_ref, payload in sorted(events, key=lambda e: e[0]):
                _, is_new = await _review_card(
                    session,
                    self._scheduler,
                    learner_id=learner_id,
                    item_id=item_id,
                    rating=rating,
                    at=at,
                    source=source,
                    source_ref=source_ref,
                    payload=payload,
                )
                applied += is_new
            return applied


class PracticeRepo:
    """The quiz and the translation task sent after each lesson, and the learner's answers to them."""

    def __init__(self, sessions: SessionFactory, scheduler: ReviewScheduler) -> None:
        self._sessions = sessions
        self._scheduler = scheduler

    async def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None:
        async with session_scope(self._sessions) as session:
            row = await session.scalar(
                select(PracticeDeliveryRow).where(
                    PracticeDeliveryRow.lesson_id == lesson_id, PracticeDeliveryRow.kind == kind
                )
            )
            return None if row is None else PracticeDeliveryRecord.model_validate(row)

    async def claim_delivery(
        self, lesson_id: int, kind: PracticeKind, item_count: int
    ) -> PracticeDeliveryRecord | None:
        """Reserve the block before sending it; None when it is already claimed (never send twice)."""
        statement = (
            insert(PracticeDeliveryRow)
            .values(lesson_id=lesson_id, kind=kind, item_count=item_count)
            .on_conflict_do_nothing(index_elements=["lesson_id", "kind"])
            .returning(PracticeDeliveryRow.id)
        )
        async with session_scope(self._sessions) as session:
            delivery_id = (await session.execute(statement)).scalar_one_or_none()
            if delivery_id is None:
                return None
            row = await session.get(PracticeDeliveryRow, delivery_id)
            return PracticeDeliveryRecord.model_validate(row)

    async def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(PracticeDeliveryRow, delivery_id)
            row.sent_at = datetime.now(UTC)
            row.telegram_message_ids = message_ids
            await session.flush()
            return PracticeDeliveryRecord.model_validate(row)

    async def add_poll(
        self,
        delivery_id: int,
        *,
        position: int,
        telegram_poll_id: str,
        telegram_message_id: int,
        item_id: int | None,
        question: QuizQuestion,
    ) -> QuizPollRecord:
        row = QuizPollRow(
            delivery_id=delivery_id,
            position=position,
            telegram_poll_id=telegram_poll_id,
            telegram_message_id=telegram_message_id,
            item_id=item_id,
            question=question.model_dump(mode="json"),
        )
        async with session_scope(self._sessions) as session:
            session.add(row)
            await session.flush()
            return QuizPollRecord.model_validate(row)

    async def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        """Store one answer; a wrong one also goes to `errors`, and the poll's item is rated for FSRS.

        None for an unknown poll or a repeated answer. One transaction: the answer, the error,
        the card and the review log are all there or none of them is.
        """
        now = datetime.now(UTC)
        async with session_scope(self._sessions) as session:
            poll = await session.scalar(select(QuizPollRow).where(QuizPollRow.telegram_poll_id == telegram_poll_id))
            learner = await session.scalar(select(LearnerRow).where(LearnerRow.telegram_chat_id == telegram_chat_id))
            if poll is None or learner is None:
                return None
            question = QuizQuestion.model_validate(poll.question)
            is_correct = option_ids == [question.correct_option]
            answer = {"poll_id": poll.id, "learner_id": learner.id, "option_ids": option_ids, "is_correct": is_correct}
            answer_id = (
                await session.execute(
                    insert(QuizAnswerRow)
                    .values(**answer, answered_at=now)
                    .on_conflict_do_nothing(index_elements=["poll_id", "learner_id"])
                    .returning(QuizAnswerRow.id)
                )
            ).scalar_one_or_none()
            if answer_id is None:
                return None
            if not is_correct:
                session.add(
                    ErrorRow(
                        learner_id=learner.id,
                        item_id=poll.item_id,
                        kind="quiz",
                        original=", ".join(question.options[i] for i in option_ids if 0 <= i < len(question.options)),
                        corrected=question.options[question.correct_option],
                        error_type=question.kind,
                        explanation_ru=question.explanation_ru,
                    )
                )
            if poll.item_id is not None:
                await _review_card(
                    session,
                    self._scheduler,
                    learner_id=learner.id,
                    item_id=poll.item_id,
                    rating=quiz_rating(is_correct),
                    at=now,
                    source="quiz",
                    source_ref=f"quiz_answer:{answer_id}",
                    payload={"poll_id": poll.id},
                )

            delivery = await session.get_one(PracticeDeliveryRow, poll.delivery_id, with_for_update=True)
            answered = (
                await session.execute(
                    select(QuizPollRow.question, QuizAnswerRow.is_correct)
                    .join(QuizAnswerRow, QuizAnswerRow.poll_id == QuizPollRow.id)
                    .where(QuizPollRow.delivery_id == delivery.id, QuizAnswerRow.learner_id == learner.id)
                    .order_by(QuizPollRow.position)
                )
            ).all()
            if delivery.completed_at is not None or len(answered) < delivery.item_count:
                return QuizResult(learner_id=learner.id, is_correct=is_correct, completed=False)
            delivery.completed_at = now
            return QuizResult(
                learner_id=learner.id,
                is_correct=is_correct,
                completed=True,
                results=[(QuizQuestion.model_validate(payload), correct) for payload, correct in answered],
            )

    async def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None:
        """The learner's latest sent translation task if it still waits for an answer (none yet, or grading failed)."""
        async with session_scope(self._sessions) as session:
            found = (
                await session.execute(
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
                )
            ).first()
            if found is None:
                return None
            delivery, lesson = found
            answer = await session.scalar(
                select(TranslationAnswerRow).where(TranslationAnswerRow.delivery_id == delivery.id)
            )
            if (answer is not None and answer.status != "failed") or lesson.lesson is None:
                return None
            return OpenTranslationTask(
                learner_id=lesson.learner_id,
                delivery=PracticeDeliveryRecord.model_validate(delivery),
                lesson=Lesson.model_validate(lesson.lesson),
            )

    async def start_translation_answer(
        self, delivery_id: int, learner_id: int, answer_text: str
    ) -> TranslationAnswerRecord | None:
        """Take the answer for grading; a failed earlier attempt is replaced. None if one is pending or graded."""
        async with session_scope(self._sessions) as session:
            row = await session.scalar(
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
            await session.flush()
            return TranslationAnswerRecord.model_validate(row)

    async def set_progress_message(self, answer_id: int, message_id: int) -> None:
        async with session_scope(self._sessions) as session:
            (await session.get_one(TranslationAnswerRow, answer_id)).progress_message_id = message_id

    async def translation_for_grading(self, answer_id: int) -> GradingTask | None:
        """The answer with its lesson and chat; None for an unknown answer or a lesson without payload."""
        async with session_scope(self._sessions) as session:
            found = (
                await session.execute(
                    select(TranslationAnswerRow, LessonRow.lesson, LearnerRow.telegram_chat_id)
                    .join(PracticeDeliveryRow, PracticeDeliveryRow.id == TranslationAnswerRow.delivery_id)
                    .join(LessonRow, LessonRow.id == PracticeDeliveryRow.lesson_id)
                    .join(LearnerRow, LearnerRow.id == TranslationAnswerRow.learner_id)
                    .where(TranslationAnswerRow.id == answer_id)
                )
            ).first()
            if found is None or found[1] is None:
                return None
            answer, lesson, chat_id = found
            return GradingTask(
                answer=TranslationAnswerRecord.model_validate(answer),
                lesson=Lesson.model_validate(lesson),
                telegram_chat_id=chat_id,
            )

    async def begin_grading(self, answer_id: int, now: datetime) -> GradingTask | None:
        """Start one grading attempt of a pending answer (counted); None if it is not pending any more."""
        async with session_scope(self._sessions) as session:
            row = await session.get(TranslationAnswerRow, answer_id, with_for_update=True)
            if row is None or row.status != "pending":
                return None
            row.grading_attempts, row.grading_started_at = row.grading_attempts + 1, now
            await session.flush()
        return await self.translation_for_grading(answer_id)

    async def stale_gradings(self, started_before: datetime, *, limit: int = 200) -> list[tuple[int, int]]:
        """(answer id, attempts) of pending answers whose last grading attempt (or arrival) is older than that."""
        started = func.coalesce(TranslationAnswerRow.grading_started_at, TranslationAnswerRow.created_at)
        async with session_scope(self._sessions) as session:
            rows = await session.execute(
                select(TranslationAnswerRow.id, TranslationAnswerRow.grading_attempts)
                .where(TranslationAnswerRow.status == "pending", started < started_before)
                .order_by(TranslationAnswerRow.created_at)
                .limit(limit)
            )
            return [(answer_id, attempts) for answer_id, attempts in rows]

    async def delivered_kinds(self, lesson_ids: list[int]) -> dict[int, set[PracticeKind]]:
        """Which practice blocks each lesson has claimed already (sent or being sent)."""
        if not lesson_ids:
            return {}
        async with session_scope(self._sessions) as session:
            rows = await session.execute(
                select(PracticeDeliveryRow.lesson_id, PracticeDeliveryRow.kind).where(
                    PracticeDeliveryRow.lesson_id.in_(lesson_ids)
                )
            )
            kinds: dict[int, set[PracticeKind]] = {}
            for lesson_id, kind in rows:
                kinds.setdefault(lesson_id, set()).add(kind)
            return kinds

    async def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord:
        """Save the grade, close the task, log every corrected sentence in `errors` and rate the lesson's item.

        Each sentence is one FSRS rating (correct -> good, fixed -> again, missing -> none).
        """
        now = datetime.now(UTC)
        async with session_scope(self._sessions) as session:
            row = await session.get_one(TranslationAnswerRow, answer_id)
            row.status, row.grade, row.error, row.graded_at = "graded", grade.model_dump(mode="json"), None, now
            delivery = await session.get_one(PracticeDeliveryRow, row.delivery_id)
            delivery.completed_at = now
            item = await _core_item(session, (await session.get_one(LessonRow, delivery.lesson_id)).topic_id)
            session.add_all(
                ErrorRow(
                    learner_id=row.learner_id,
                    item_id=None if item is None else item.id,
                    kind="text",
                    original=sentence.learner_text,
                    corrected=sentence.corrected,
                    error_type=sentence.error_type,
                    explanation_ru=sentence.explanation_ru,
                )
                for sentence in grade.sentences
                if sentence.verdict == "fixed"
            )
            for sentence in grade.sentences:
                rating = translation_rating(sentence.verdict)
                if item is None or rating is None:
                    continue
                await _review_card(
                    session,
                    self._scheduler,
                    learner_id=row.learner_id,
                    item_id=item.id,
                    rating=rating,
                    at=now,
                    source="translation",
                    source_ref=f"translation_answer:{row.id}:{sentence.n}",
                    payload={"verdict": sentence.verdict},
                )
            await session.flush()
            return TranslationAnswerRecord.model_validate(row)

    async def fail_translation_answer(self, answer_id: int, reason: str) -> None:
        async with session_scope(self._sessions) as session:
            row = await session.get_one(TranslationAnswerRow, answer_id)
            row.status, row.error = "failed", reason

    async def dispute(self, answer_id: int) -> bool:
        """Flag a grade the learner disagrees with; False for an unknown or ungraded answer."""
        async with session_scope(self._sessions) as session:
            row = await session.get(TranslationAnswerRow, answer_id)
            if row is None or row.status != "graded":
                return False
            row.disputed = True
            return True
