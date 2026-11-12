"""In-memory stand-ins for the storage repositories, for tests that need no database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.profile import LearnerProfile
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import (
    CardRecord,
    ClaimOutcome,
    ItemDraft,
    ItemRecord,
    LearnerProfileRecord,
    LearnerRecord,
    LessonContentRecord,
    LessonRecord,
    LessonStatus,
    OpenTranslationTask,
    PracticeDeliveryRecord,
    PracticeKind,
    ProfileInputs,
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

    def get_by_id(self, learner_id: int) -> LearnerRecord | None:
        return next((learner for learner in self.by_chat.values() if learner.id == learner_id), None)

    def get_or_create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord:
        if telegram_chat_id not in self.by_chat:
            now = datetime.now(UTC)
            self.by_chat[telegram_chat_id] = LearnerRecord(
                id=len(self.by_chat) + 1,
                telegram_chat_id=telegram_chat_id,
                cefr=default_cefr,
                created_at=now,
                updated_at=now,
                **(schedule or LearnerSchedule()).model_dump(),
            )
        return self.by_chat[telegram_chat_id]

    def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord:
        learner = self.get_or_create(telegram_chat_id, cefr).model_copy(update={"cefr": cefr})
        self.by_chat[telegram_chat_id] = learner
        return learner

    def list_active(self) -> list[LearnerRecord]:
        return sorted((r for r in self.by_chat.values() if r.receives_lessons), key=lambda r: r.id)

    def _change(self, telegram_chat_id: str, **changes: Any) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        if learner is None:
            return None
        self.by_chat[telegram_chat_id] = learner.model_copy(update=changes)
        return self.by_chat[telegram_chat_id]

    def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None:
        return self._change(telegram_chat_id, **schedule.model_dump())

    def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        started = on if learner is None or learner.curriculum_started_on is None else learner.curriculum_started_on
        return self._change(telegram_chat_id, curriculum_started_on=started, is_active=True, deactivated_reason=None)

    def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        paused_at = (learner.paused_at if learner else None) or datetime.now(UTC) if paused else None
        return self._change(telegram_chat_id, paused_at=paused_at)

    def deactivate(self, telegram_chat_id: str, reason: str) -> None:
        self._change(telegram_chat_id, is_active=False, deactivated_reason=reason)

    def started(self, telegram_chat_id: str, on: date, cefr: Cefr = "A2", **changes: Any) -> LearnerRecord:
        """Test helper: a learner whose course started on `on`."""
        self.get_or_create(telegram_chat_id, cefr)
        learner = self._change(telegram_chat_id, curriculum_started_on=on, **changes)
        assert learner is not None
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
        self,
        learner_id: int,
        day: date,
        lesson: Lesson,
        message_ids: list[int],
        *,
        curriculum_version: int | None,
        content_id: int | None = None,
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
            content_id=content_id,
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

    def list_sent(self) -> list[LessonRecord]:
        sent = (record for record in self.rows.values() if record.status == "sent")
        return sorted(sent, key=lambda record: (record.lesson_date, record.id))

    def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]:
        return {key: self.rows[key].status for key in keys if key in self.rows}

    def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]:
        return {
            r.topic_id
            for (lid, _), r in self.rows.items()
            if lid == learner_id and r.cefr == cefr and r.status == "sent"
        }

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
        content_id: int | None = None,
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
            content_id=content_id,
        )
        self.rows[(learner_id, day)] = record
        return record


class FakeContents:
    """Shared lesson content with the same claim rules as `LessonContentRepo`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int, str], LessonContentRecord] = {}

    def claim(
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
        key = (topic_id, cefr, curriculum_version, content_key)
        row = self.rows.get(key)
        if row is None:
            row = LessonContentRecord(
                id=len(self.rows) + 1,
                topic_id=topic_id,
                cefr=cefr,
                curriculum_version=curriculum_version,
                content_key=content_key,
                status="generating",
                schema_version=SCHEMA_VERSIONS["lesson"],
                lesson=None,
                error=None,
                attempts=1,
                claimed_at=now,
                claimed_by=owner,
                generated_at=None,
                created_at=now,
            )
            self.rows[key] = row
            return "claimed", row
        if row.status == "ready":
            return "ready", row
        fresh = row.claimed_at is not None and row.claimed_at > now - stale_after
        if row.attempts >= max_attempts and (row.status == "failed" or not fresh):
            self.rows[key] = row.model_copy(update={"status": "failed"})
            return "exhausted", self.rows[key]
        if fresh:
            return "busy", row
        changes = {"status": "generating", "error": None, "attempts": row.attempts + 1, "claimed_at": now}
        self.rows[key] = row.model_copy(update=changes | {"claimed_by": owner})
        return "claimed", self.rows[key]

    def _update(self, content_id: int, **changes: Any) -> LessonContentRecord:
        key, row = next((k, r) for k, r in self.rows.items() if r.id == content_id)
        self.rows[key] = row.model_copy(update=changes)
        return self.rows[key]

    def finish(self, content_id: int, lesson: Lesson) -> LessonContentRecord:
        return self._update(content_id, status="ready", lesson=lesson, error=None, generated_at=datetime.now(UTC))

    def fail(self, content_id: int, reason: str) -> LessonContentRecord:
        return self._update(content_id, status="failed", error=reason)

    def unavailable_topics(
        self, cefr: Cefr, curriculum_version: int, content_key: str, *, max_attempts: int
    ) -> set[str]:
        return {
            topic_id
            for (topic_id, level, version, key), row in self.rows.items()
            if (level, version, key) == (cefr, curriculum_version, content_key)
            and row.status == "failed"
            and row.attempts >= max_attempts
        }


class FakeItems:
    def __init__(self) -> None:
        self.by_key: dict[str, ItemRecord] = {}
        self.failure: Exception | None = None  # raised by get_or_create, to test callers' failure handling

    def get_by_key(self, key: str) -> ItemRecord | None:
        return self.by_key.get(key)

    def get_or_create(self, draft: ItemDraft, *, source_lesson_id: int | None = None) -> tuple[ItemRecord, bool]:
        if self.failure is not None:
            raise self.failure
        if draft.key in self.by_key:
            return self.by_key[draft.key], False
        record = ItemRecord(
            id=len(self.by_key) + 1,
            **draft.model_dump(),
            version=1,
            source_lesson_id=source_lesson_id,
            created_at=datetime.now(UTC),
        )
        self.by_key[draft.key] = record
        return record, True


class FakeCards:
    """Cards without FSRS: a review bumps the counter and pushes `due_at` one day out."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[int, int], CardRecord] = {}
        self.reviews: list[tuple[int, int, int, str]] = []  # (learner id, item id, rating, source_ref)

    def get(self, learner_id: int, item_id: int) -> CardRecord | None:
        return self.by_key.get((learner_id, item_id))

    def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]:
        due = [c for c in self.by_key.values() if c.learner_id == learner_id and c.due_at <= now]
        return sorted(due, key=lambda c: c.due_at)[:limit]

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
        if any(ref == source_ref for *_, ref in self.reviews):
            return self.by_key[(learner_id, item_id)]
        card = self.by_key.get((learner_id, item_id)) or CardRecord(
            id=len(self.by_key) + 1,
            learner_id=learner_id,
            item_id=item_id,
            fsrs_state={},
            due_at=at,
            last_reviewed_at=None,
            reviews=0,
            created_at=at,
        )
        card = card.model_copy(
            update={"due_at": at + timedelta(days=1), "last_reviewed_at": at, "reviews": card.reviews + 1}
        )
        self.by_key[(learner_id, item_id)] = card
        self.reviews.append((learner_id, item_id, rating, source_ref))
        return card


class FakeProfiles:
    def __init__(self, learners: FakeLearners) -> None:
        self._learners = learners
        self.versions: dict[int, list[LearnerProfileRecord]] = {}
        self.next_inputs: ProfileInputs | None = None  # what `inputs` returns next, when a test sets it

    def latest(self, learner_id: int) -> LearnerProfileRecord | None:
        versions = self.versions.get(learner_id)
        return versions[-1] if versions else None

    def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord:
        versions = self.versions.setdefault(learner_id, [])
        record = LearnerProfileRecord(
            id=sum(len(v) for v in self.versions.values()) + 1,
            learner_id=learner_id,
            version=len(versions) + 1,
            schema_version=SCHEMA_VERSIONS["learner_profile"],
            profile=profile,
            created_at=datetime.now(UTC),
        )
        versions.append(record)
        return record

    def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs:
        if self.next_inputs is not None:
            return self.next_inputs
        learner = next(record for record in self._learners.by_chat.values() if record.id == learner_id)
        return ProfileInputs(cefr=learner.cefr, window_days=window_days)


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
        item_id: int | None,
        question: QuizQuestion,
    ) -> QuizPollRecord:
        record = QuizPollRecord(
            id=len(self.polls) + 1,
            delivery_id=delivery_id,
            position=position,
            telegram_poll_id=telegram_poll_id,
            telegram_message_id=telegram_message_id,
            item_id=item_id,
            question=question,
            created_at=datetime.now(UTC),
        )
        self.polls[telegram_poll_id] = record
        return record

    def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        poll = self.polls.get(telegram_poll_id)
        learner = self._learners.get(telegram_chat_id)
        if poll is None or learner is None:
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
            return QuizResult(learner_id=learner.id, is_correct=is_correct, completed=False)
        self.deliveries[delivery.id] = delivery.model_copy(update={"completed_at": datetime.now(UTC)})
        return QuizResult(
            learner_id=learner.id,
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
    stores = Stores(
        learners=learners,
        lessons=lessons,
        practice=FakePractice(learners, lessons),
        items=FakeItems(),
        cards=FakeCards(),
        profiles=FakeProfiles(learners),
        contents=FakeContents(),
    )
    return stores, learners, lessons


def fake_practice(stores: Stores) -> FakePractice:
    assert isinstance(stores.practice, FakePractice)
    return stores.practice


def fake_items(stores: Stores) -> FakeItems:
    assert isinstance(stores.items, FakeItems)
    return stores.items


def fake_contents(stores: Stores) -> FakeContents:
    assert isinstance(stores.contents, FakeContents)
    return stores.contents


def fake_profiles(stores: Stores) -> FakeProfiles:
    assert isinstance(stores.profiles, FakeProfiles)
    return stores.profiles
