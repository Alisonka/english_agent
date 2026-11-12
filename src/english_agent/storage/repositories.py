"""Repositories: the only code that touches ORM rows. They take and return pydantic records.

Each method runs in its own short transaction (see `session_scope`), so one repository
can be shared by the polling thread and the scheduler thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from english_agent.config import Settings
from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.profile import LearnerProfile
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import (
    CORE_SLUG,
    CardRecord,
    ErrorRecord,
    ItemDraft,
    ItemRecord,
    LearnerProfileRecord,
    LearnerRecord,
    LessonRecord,
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
    def get(self, telegram_chat_id: str) -> LearnerRecord | None: ...

    def get_by_id(self, learner_id: int) -> LearnerRecord | None: ...

    def get_or_create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord: ...

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord: ...

    def list_active(self) -> list[LearnerRecord]: ...

    def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None: ...

    def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None: ...

    def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None: ...

    def deactivate(self, telegram_chat_id: str, reason: str) -> None: ...


class LessonStore(Protocol):
    def get(self, learner_id: int, day: date) -> LessonRecord | None: ...

    def sent_on(self, learner_id: int, day: date) -> bool: ...

    def record_sent(
        self, learner_id: int, day: date, lesson: Lesson, message_ids: list[int], *, curriculum_version: int | None
    ) -> LessonRecord: ...

    def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord: ...

    def list_sent(self) -> list[LessonRecord]: ...

    def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]: ...

    def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]: ...


class ItemStore(Protocol):
    def get_by_key(self, key: str) -> ItemRecord | None: ...

    def get_or_create(self, draft: ItemDraft, *, source_lesson_id: int | None = None) -> tuple[ItemRecord, bool]: ...


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
        item_id: int | None,
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


class CardStore(Protocol):
    def get(self, learner_id: int, item_id: int) -> CardRecord | None: ...

    def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]: ...

    def review(
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
    def latest(self, learner_id: int) -> LearnerProfileRecord | None: ...

    def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord: ...

    def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs: ...


@dataclass(frozen=True)
class Stores:
    learners: LearnerStore
    lessons: LessonStore
    practice: PracticeStore
    items: ItemStore
    cards: CardStore
    profiles: ProfileStore


def sql_stores(sessions: SessionFactory, scheduler: ReviewScheduler | None = None) -> Stores:
    scheduler = scheduler or ReviewScheduler()
    return Stores(
        learners=LearnerRepo(sessions),
        lessons=LessonRepo(sessions),
        practice=PracticeRepo(sessions, scheduler),
        items=ItemRepo(sessions),
        cards=CardRepo(sessions, scheduler),
        profiles=ProfileRepo(sessions),
    )


def open_stores(settings: Settings) -> Stores | None:
    """SQL-backed stores for DATABASE_URL, or None when it is not set."""
    if not settings.database_url.get_secret_value():
        return None
    return sql_stores(make_session_factory(make_engine(settings)), make_scheduler(settings))


def _review_card(
    session: Session,
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
    card = session.scalar(locked)
    if card is None:
        # ON CONFLICT: the polling and the grading thread may rate the same new item at once.
        session.execute(
            insert(CardRow)
            .values(learner_id=learner_id, item_id=item_id, fsrs_state=scheduler.new_card(at), due_at=at)
            .on_conflict_do_nothing(index_elements=["learner_id", "item_id"])
        )
        card = session.scalar(locked)
        assert card is not None
    # Checked under the card lock, so two threads with the same event cannot both apply it.
    if session.scalar(select(ReviewLogRow.id).where(ReviewLogRow.source_ref == source_ref)) is not None:
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
    session.flush()
    return card, True


def _core_item(session: Session, topic_id: str) -> ItemRow | None:
    return session.scalar(select(ItemRow).where(ItemRow.key == core_item_key(topic_id)))


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

    def get_by_id(self, learner_id: int) -> LearnerRecord | None:
        with session_scope(self._sessions) as session:
            row = session.get(LearnerRow, learner_id)
            return None if row is None else LearnerRecord.model_validate(row)

    def get_or_create(
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

    def list_active(self) -> list[LearnerRecord]:
        """Learners who may get lessons: started, not paused, not deactivated. Oldest first."""
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(LearnerRow)
                .where(
                    LearnerRow.is_active.is_(True),
                    LearnerRow.paused_at.is_(None),
                    LearnerRow.curriculum_started_on.is_not(None),
                )
                .order_by(LearnerRow.id)
            )
            return [LearnerRecord.model_validate(row) for row in rows]

    def _change(self, telegram_chat_id: str, **values: Any) -> LearnerRecord | None:
        statement = (
            update(LearnerRow)
            .where(LearnerRow.telegram_chat_id == telegram_chat_id)
            .values(**values, updated_at=func.now())
            .returning(LearnerRow)
        )
        with session_scope(self._sessions) as session:
            row = session.scalar(statement)
            return None if row is None else LearnerRecord.model_validate(row)

    def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None:
        """Replace the learner's schedule; None for an unknown chat."""
        return self._change(telegram_chat_id, **schedule.model_dump())

    def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None:
        """/start: (re)activate the learner; the course start is set once and then kept."""
        return self._change(
            telegram_chat_id,
            curriculum_started_on=func.coalesce(LearnerRow.curriculum_started_on, on),
            is_active=True,
            deactivated_reason=None,
        )

    def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None:
        # COALESCE: pausing twice keeps the original pause time.
        paused_at = func.coalesce(LearnerRow.paused_at, func.now()) if paused else None
        return self._change(telegram_chat_id, paused_at=paused_at)

    def deactivate(self, telegram_chat_id: str, reason: str) -> None:
        """Stop all sending to the chat (the bot was blocked, the course is over) until /start."""
        self._change(telegram_chat_id, is_active=False, deactivated_reason=reason)


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

    def list_sent(self) -> list[LessonRecord]:
        """Every sent lesson, oldest first. For maintenance commands: one lesson a day, so no paging."""
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(LessonRow).where(LessonRow.status == "sent").order_by(LessonRow.lesson_date, LessonRow.id)
            )
            return [LessonRecord.model_validate(row) for row in rows]

    def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]:
        """The status of each (learner id, day) that has a row; one query, no payloads."""
        if not keys:
            return {}
        with session_scope(self._sessions) as session:
            rows = session.execute(
                select(LessonRow.learner_id, LessonRow.lesson_date, LessonRow.status).where(
                    tuple_(LessonRow.learner_id, LessonRow.lesson_date).in_(keys)
                )
            )
            return {(learner_id, day): status for learner_id, day, status in rows}

    def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]:
        """Topics of this level the learner already got: the next lesson is the first topic not in here."""
        with session_scope(self._sessions) as session:
            rows = session.scalars(
                select(LessonRow.topic_id).where(
                    LessonRow.learner_id == learner_id, LessonRow.cefr == cefr, LessonRow.status == "sent"
                )
            )
            return set(rows)

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
    """Learnable items: the core item of every taught topic (see `english_agent.items`)."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    def _find(session: Session, key: str) -> ItemRow | None:
        return session.scalar(select(ItemRow).where(ItemRow.key == key))

    def get_by_key(self, key: str) -> ItemRecord | None:
        with session_scope(self._sessions) as session:
            row = self._find(session, key)
            return None if row is None else ItemRecord.model_validate(row)

    def get_or_create(self, draft: ItemDraft, *, source_lesson_id: int | None = None) -> tuple[ItemRecord, bool]:
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
        with session_scope(self._sessions) as session:
            created_id = session.execute(statement).scalar_one_or_none()
            row = self._find(session, draft.key)
            assert row is not None
            return ItemRecord.model_validate(row), created_id is not None


class CardRepo:
    """FSRS cards, one per learner and item. Practice rates them itself; this is for readers and replays."""

    def __init__(self, sessions: SessionFactory, scheduler: ReviewScheduler) -> None:
        self._sessions = sessions
        self._scheduler = scheduler

    def get(self, learner_id: int, item_id: int) -> CardRecord | None:
        with session_scope(self._sessions) as session:
            row = session.scalar(select(CardRow).where(CardRow.learner_id == learner_id, CardRow.item_id == item_id))
            return None if row is None else CardRecord.model_validate(row)

    def review(
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
        with session_scope(self._sessions) as session:
            row, _ = _review_card(
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


class ProfileRepo:
    """Versions of the learner profile, and the raw numbers a new version is computed from."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def latest(self, learner_id: int) -> LearnerProfileRecord | None:
        with session_scope(self._sessions) as session:
            row = session.scalar(
                select(LearnerProfileRow)
                .where(LearnerProfileRow.learner_id == learner_id)
                .order_by(LearnerProfileRow.version.desc())
                .limit(1)
            )
            return None if row is None else LearnerProfileRecord.model_validate(row)

    def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord:
        with session_scope(self._sessions) as session:
            # The learner row lock serialises appends from the polling and the grading thread.
            session.get_one(LearnerRow, learner_id, with_for_update=True)
            current = session.scalar(
                select(func.max(LearnerProfileRow.version)).where(LearnerProfileRow.learner_id == learner_id)
            )
            row = LearnerProfileRow(
                learner_id=learner_id,
                version=(current or 0) + 1,
                schema_version=SCHEMA_VERSIONS["learner_profile"],
                profile=profile.model_dump(mode="json"),
            )
            session.add(row)
            session.flush()
            return LearnerProfileRecord.model_validate(row)

    def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs:
        since = now - timedelta(days=window_days)
        with session_scope(self._sessions) as session:
            learner = session.get_one(LearnerRow, learner_id)
            lessons_sent = session.scalar(
                select(func.count())
                .select_from(LessonRow)
                .where(LessonRow.learner_id == learner_id, LessonRow.status == "sent")
            )
            errors = session.scalars(
                select(ErrorRow).where(ErrorRow.learner_id == learner_id, ErrorRow.created_at >= since)
            ).all()
            item_ids = {error.item_id for error in errors if error.item_id is not None}
            items = session.scalars(select(ItemRow).where(ItemRow.id.in_(item_ids))).all() if item_ids else []
            quiz_answers, quiz_correct = session.execute(
                select(func.count(), func.count().filter(QuizAnswerRow.is_correct.is_(True)))
                .select_from(QuizAnswerRow)
                .where(QuizAnswerRow.learner_id == learner_id, QuizAnswerRow.answered_at >= since)
            ).one()
            grades = session.scalars(
                select(TranslationAnswerRow.grade).where(
                    TranslationAnswerRow.learner_id == learner_id,
                    TranslationAnswerRow.status == "graded",
                    TranslationAnswerRow.graded_at >= since,
                )
            ).all()
            cards, due_cards = session.execute(
                select(func.count(), func.count().filter(CardRow.due_at <= now))
                .select_from(CardRow)
                .where(CardRow.learner_id == learner_id)
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

    def link_polls_to_items(self) -> int:
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
        with session_scope(self._sessions) as session:
            return session.execute(statement).rowcount

    def replay_reviews(self) -> int:
        """Rate every stored quiz answer and graded translation that has no review log yet, oldest first."""
        events: list[tuple[datetime, int, int, int, str, str, dict[str, Any]]] = []
        with session_scope(self._sessions) as session:
            answers = session.execute(
                select(QuizAnswerRow, QuizPollRow.item_id)
                .join(QuizPollRow, QuizPollRow.id == QuizAnswerRow.poll_id)
                .where(QuizPollRow.item_id.is_not(None))
            ).all()
            for answer, item_id in answers:
                rating = quiz_rating(answer.is_correct)
                ref = f"quiz_answer:{answer.id}"
                payload = {"poll_id": answer.poll_id}
                events.append((answer.answered_at, answer.learner_id, item_id, rating, "quiz", ref, payload))
            graded = session.execute(
                select(TranslationAnswerRow, LessonRow.topic_id)
                .join(PracticeDeliveryRow, PracticeDeliveryRow.id == TranslationAnswerRow.delivery_id)
                .join(LessonRow, LessonRow.id == PracticeDeliveryRow.lesson_id)
                .where(TranslationAnswerRow.status == "graded", TranslationAnswerRow.grade.is_not(None))
            ).all()
            for answer, topic_id in graded:
                item = _core_item(session, topic_id)
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
                _, is_new = _review_card(
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
        with session_scope(self._sessions) as session:
            session.add(row)
            session.flush()
            return QuizPollRecord.model_validate(row)

    def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        """Store one answer; a wrong one also goes to `errors`, and the poll's item is rated for FSRS.

        None for an unknown poll or a repeated answer. One transaction: the answer, the error,
        the card and the review log are all there or none of them is.
        """
        now = datetime.now(UTC)
        with session_scope(self._sessions) as session:
            poll = session.scalar(select(QuizPollRow).where(QuizPollRow.telegram_poll_id == telegram_poll_id))
            learner = session.scalar(select(LearnerRow).where(LearnerRow.telegram_chat_id == telegram_chat_id))
            if poll is None or learner is None:
                return None
            question = QuizQuestion.model_validate(poll.question)
            is_correct = option_ids == [question.correct_option]
            answer = {"poll_id": poll.id, "learner_id": learner.id, "option_ids": option_ids, "is_correct": is_correct}
            answer_id = session.execute(
                insert(QuizAnswerRow)
                .values(**answer, answered_at=now)
                .on_conflict_do_nothing(index_elements=["poll_id", "learner_id"])
                .returning(QuizAnswerRow.id)
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
                _review_card(
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

            delivery = session.get_one(PracticeDeliveryRow, poll.delivery_id, with_for_update=True)
            answered = session.execute(
                select(QuizPollRow.question, QuizAnswerRow.is_correct)
                .join(QuizAnswerRow, QuizAnswerRow.poll_id == QuizPollRow.id)
                .where(QuizPollRow.delivery_id == delivery.id, QuizAnswerRow.learner_id == learner.id)
                .order_by(QuizPollRow.position)
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
        """Save the grade, close the task, log every corrected sentence in `errors` and rate the lesson's item.

        Each sentence is one FSRS rating (correct -> good, fixed -> again, missing -> none).
        """
        now = datetime.now(UTC)
        with session_scope(self._sessions) as session:
            row = session.get_one(TranslationAnswerRow, answer_id)
            row.status, row.grade, row.error, row.graded_at = "graded", grade.model_dump(mode="json"), None, now
            delivery = session.get_one(PracticeDeliveryRow, row.delivery_id)
            delivery.completed_at = now
            item = _core_item(session, session.get_one(LessonRow, delivery.lesson_id).topic_id)
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
                _review_card(
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
