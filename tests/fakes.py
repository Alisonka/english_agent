"""In-memory stand-ins for the storage repositories, the model and the research tools: no database, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from english_agent.llm import AssistantTurn
from english_agent.models.cefr import Cefr
from english_agent.models.content_order import ContentBrief, ContentOrder, OrderStatus
from english_agent.models.exam import ExamAnswer, ExamKind, ExamRecord, ExamReport, PoolQuestion
from english_agent.models.exercise import BankExercise
from english_agent.models.knowledge import LearnerAbility, QuestionStat
from english_agent.models.lesson import Lesson
from english_agent.models.plan import DayPlan, DayPlanRecord
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.profile import LearnerProfile
from english_agent.models.run import RunReport
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.storage import (
    AskedHistory,
    CardMemory,
    CardRecord,
    ClaimOutcome,
    ErrorRecord,
    EvidenceRecord,
    GradingInput,
    GradingSentence,
    GradingTask,
    ItemAccuracy,
    ItemDraft,
    ItemRecord,
    LearnerGoal,
    LearnerProfileRecord,
    LearnerRecord,
    LessonClaim,
    LessonContentRecord,
    LessonRecord,
    LessonState,
    LessonStatus,
    OpenTranslationTask,
    PracticeBlock,
    PracticeDeliveryRecord,
    PracticeKind,
    ProfileInputs,
    QuizPollRecord,
    QuizResult,
    SentStep,
    TranslationAnswerRecord,
)
from english_agent.models.topic import LessonStep, TopicContent
from english_agent.research.models import PageContent, SearchHit
from english_agent.research.sites import Site
from english_agent.research.tools import rank_urls
from english_agent.storage.repositories import LessonAlreadySentError, Stores, _taught_columns
from english_agent.versions import SCHEMA_VERSIONS


def _fake_grading_input(record: LessonRecord, delivery: PracticeDeliveryRecord) -> GradingInput | None:
    block = PracticeBlock.model_validate(delivery.payload) if delivery.payload else None
    if block is not None and block.sentences:
        step = record.step
        return GradingInput(
            title_en=step.title_en if step is not None else record.topic_id,
            title_ru=step.title_ru if step is not None else record.topic_id,
            cefr=record.cefr,
            topic_id=record.topic_id,
            sentences=[
                GradingSentence(
                    n=index,
                    ru=chosen.task.ru,
                    reference_en=chosen.task.reference_en,
                    focus_ru=chosen.task.focus_ru,
                    key_idea_ru=chosen.key_idea_ru,
                    form=chosen.form,
                )
                for index, chosen in enumerate(block.sentences, start=1)
            ],
        )
    lesson = record.lesson
    if lesson is None or not lesson.translation:
        return None
    return GradingInput(
        title_en=lesson.title_en,
        title_ru=lesson.title_ru,
        cefr=lesson.cefr,
        topic_id=lesson.topic_id,
        sentences=[
            GradingSentence(
                n=index,
                ru=task.ru,
                reference_en=task.reference_en,
                focus_ru=task.focus_ru,
                key_idea_ru=lesson.key_idea_ru,
                form=lesson.form,
            )
            for index, task in enumerate(lesson.translation, start=1)
        ],
    )


class FakeLearners:
    def __init__(self) -> None:
        self.by_chat: dict[str, LearnerRecord] = {}

    async def get(self, telegram_chat_id: str) -> LearnerRecord | None:
        return self.by_chat.get(telegram_chat_id)

    async def get_by_id(self, learner_id: int) -> LearnerRecord | None:
        return next((learner for learner in self.by_chat.values() if learner.id == learner_id), None)

    async def get_or_create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord:
        return self.create(telegram_chat_id, default_cefr, schedule)

    def create(
        self, telegram_chat_id: str, default_cefr: Cefr, schedule: LearnerSchedule | None = None
    ) -> LearnerRecord:
        """Test helper, and the body of `get_or_create`."""
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

    async def set_cefr(self, telegram_chat_id: str, cefr: Cefr) -> LearnerRecord:
        learner = self.create(telegram_chat_id, cefr).model_copy(update={"cefr": cefr})
        self.by_chat[telegram_chat_id] = learner
        return learner

    async def list_active(self) -> list[LearnerRecord]:
        return sorted((r for r in self.by_chat.values() if r.receives_lessons), key=lambda r: r.id)

    def _change(self, telegram_chat_id: str, **changes: Any) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        if learner is None:
            return None
        self.by_chat[telegram_chat_id] = learner.model_copy(update=changes)
        return self.by_chat[telegram_chat_id]

    async def set_schedule(self, telegram_chat_id: str, schedule: LearnerSchedule) -> LearnerRecord | None:
        return self._change(telegram_chat_id, **schedule.model_dump())

    async def start_course(self, telegram_chat_id: str, on: date) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        started = on if learner is None or learner.curriculum_started_on is None else learner.curriculum_started_on
        return self._change(telegram_chat_id, curriculum_started_on=started, is_active=True, deactivated_reason=None)

    async def set_paused(self, telegram_chat_id: str, paused: bool) -> LearnerRecord | None:
        learner = self.by_chat.get(telegram_chat_id)
        paused_at = (learner.paused_at if learner else None) or datetime.now(UTC) if paused else None
        return self._change(telegram_chat_id, paused_at=paused_at)

    async def deactivate(self, telegram_chat_id: str, reason: str) -> None:
        self._change(telegram_chat_id, is_active=False, deactivated_reason=reason)

    async def set_goal(self, telegram_chat_id: str, goal: LearnerGoal) -> LearnerRecord | None:
        return self._change(telegram_chat_id, goal=goal)

    async def set_interests(self, telegram_chat_id: str, interests: list[str]) -> LearnerRecord | None:
        return self._change(telegram_chat_id, interests=interests)

    def started(self, telegram_chat_id: str, on: date, cefr: Cefr = "A2", **changes: Any) -> LearnerRecord:
        """Test helper: a learner whose course started on `on`."""
        self.create(telegram_chat_id, cefr)
        learner = self._change(telegram_chat_id, curriculum_started_on=on, **changes)
        assert learner is not None
        return learner


class FakeLessons:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, date], LessonRecord] = {}

    async def get(self, learner_id: int, day: date) -> LessonRecord | None:
        return self.rows.get((learner_id, day))

    async def sent_on(self, learner_id: int, day: date) -> bool:
        return self.is_sent(learner_id, day)

    def is_sent(self, learner_id: int, day: date) -> bool:
        row = self.rows.get((learner_id, day))
        return row is not None and row.status == "sent"

    async def record_sent(
        self,
        learner_id: int,
        day: date,
        taught: Lesson | LessonStep,
        message_ids: list[int],
        *,
        curriculum_version: int | None,
        content_id: int | None = None,
    ) -> LessonRecord:
        return self._save(
            learner_id,
            day,
            curriculum_version=curriculum_version,
            status="sent",
            error=None,
            message_ids=message_ids,
            content_id=content_id,
            **_taught_columns(taught),
        )

    async def record_failed(
        self, learner_id: int, day: date, *, topic_id: str, cefr: Cefr, reason: str, curriculum_version: int | None
    ) -> LessonRecord:
        return self._save(
            learner_id,
            day,
            topic_id=topic_id,
            cefr=cefr,
            curriculum_version=curriculum_version,
            status="failed",
            payload=None,
            payload_kind="lesson",
            schema_version=SCHEMA_VERSIONS["lesson"],
            error=reason,
            message_ids=[],
        )

    async def list_sent(self) -> list[LessonRecord]:
        sent = (record for record in self.rows.values() if record.status == "sent")
        return sorted(sent, key=lambda record: (record.lesson_date, record.id))

    async def statuses(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonStatus]:
        return {key: self.rows[key].status for key in keys if key in self.rows}

    async def states(self, keys: list[tuple[int, date]]) -> dict[tuple[int, date], LessonState]:
        return {
            key: LessonState(
                lesson_id=r.id, status=r.status, attempts=r.attempts, claimed_at=r.claimed_at, sent_at=r.sent_at
            )
            for key in keys
            if (r := self.rows.get(key)) is not None
        }

    def _by_id(self, lesson_id: int) -> tuple[tuple[int, date], LessonRecord]:
        return next((key, r) for key, r in self.rows.items() if r.id == lesson_id)

    def _set(self, lesson_id: int, **changes: Any) -> LessonRecord:
        key, row = self._by_id(lesson_id)
        self.rows[key] = row.model_copy(update=changes)
        return self.rows[key]

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
        row = self.rows.get((learner_id, day))
        if row is None:
            row = LessonRecord(
                id=max((r.id for r in self.rows.values()), default=0) + 1,
                learner_id=learner_id,
                lesson_date=day,
                topic_id=topic_id,
                cefr=cefr,
                curriculum_version=curriculum_version,
                schema_version=SCHEMA_VERSIONS["lesson"],
                status="pending",
                payload=None,
                error=None,
                telegram_message_ids=[],
                created_at=now,
                sent_at=None,
                claimed_at=now,
                attempts=1,
            )
            self.rows[(learner_id, day)] = row
            return "claimed", row
        fresh = row.claimed_at is not None and row.claimed_at > now - stale_after
        if row.status in ("sent", "skipped"):
            return "done", row
        if row.status in ("pending", "sending") and fresh:
            return "busy", row
        if row.attempts >= max_attempts:
            return "done", self._set(row.id, status="skipped")
        outcome: LessonClaim = "resume" if row.status == "sending" else "claimed"
        changes: dict[str, Any] = {"attempts": row.attempts + 1, "claimed_at": now}
        if outcome == "claimed":
            changes |= {
                "topic_id": topic_id,
                "cefr": cefr,
                "curriculum_version": curriculum_version,
                "status": "pending",
            }
        return outcome, self._set(row.id, **changes)

    async def release(self, lesson_id: int) -> None:
        _, row = self._by_id(lesson_id)
        if row.status == "pending":
            self._set(lesson_id, claimed_at=None, attempts=max(row.attempts - 1, 0))

    async def start_sending(self, lesson_id: int, content_id: int, taught: Lesson | LessonStep) -> LessonRecord:
        return self._set(lesson_id, status="sending", content_id=content_id, error=None, **_taught_columns(taught))

    async def add_message_id(self, lesson_id: int, message_id: int) -> None:
        _, row = self._by_id(lesson_id)
        self._set(lesson_id, telegram_message_ids=[*row.telegram_message_ids, message_id])

    async def finish_sending(self, lesson_id: int) -> LessonRecord:
        return self._set(lesson_id, status="sent", sent_at=datetime.now(UTC), claimed_at=None)

    async def fail(self, lesson_id: int, reason: str, *, final: bool) -> LessonRecord:
        _, row = self._by_id(lesson_id)
        if row.status == "sent":
            return row
        return self._set(lesson_id, status="skipped" if final else "failed", error=reason)

    async def close_past(self, before: date) -> int:
        stale = [
            r for r in self.rows.values() if r.lesson_date < before and r.status in ("pending", "sending", "failed")
        ]
        for row in stale:
            self._set(row.id, status="skipped", claimed_at=None)
        return len(stale)

    async def sent_topic_ids(self, learner_id: int, cefr: Cefr) -> set[str]:
        return {
            r.topic_id
            for (lid, _), r in self.rows.items()
            if lid == learner_id and r.cefr == cefr and r.status == "sent"
        }

    async def sent_steps(self, learner_id: int, cefr: Cefr) -> list[SentStep]:
        sent = [r for (lid, _), r in self.rows.items() if lid == learner_id and r.cefr == cefr and r.status == "sent"]
        sent.sort(key=lambda r: (r.lesson_date, r.id))
        return [
            SentStep(
                lesson_id=r.id,
                topic_id=r.topic_id,
                lesson_date=r.lesson_date,
                step_index=r.step_index,
                step_count=r.step_count,
                step_slug=r.step_slug,
                variant=r.variant,
                content_id=r.content_id,
            )
            for r in sent
        ]

    async def taught_step(self, learner_id: int, item_key: str) -> LessonRecord | None:
        topic_id, _, slug = item_key.partition(":")
        matches = [
            r
            for (lid, _), r in self.rows.items()
            if lid == learner_id
            and r.topic_id == topic_id
            and r.step_slug == slug
            and r.variant == "main"
            and r.status == "sent"
        ]
        matches.sort(key=lambda r: (r.lesson_date, r.id))
        return matches[-1] if matches else None

    def _save(
        self,
        learner_id: int,
        day: date,
        *,
        status: LessonStatus,
        error: str | None,
        message_ids: list[int],
        curriculum_version: int | None,
        content_id: int | None = None,
        **columns: Any,
    ) -> LessonRecord:
        if self.is_sent(learner_id, day):
            raise LessonAlreadySentError(f"{learner_id} {day}")
        now = datetime.now(UTC)
        record = LessonRecord(
            id=len(self.rows) + 1,
            learner_id=learner_id,
            lesson_date=day,
            curriculum_version=curriculum_version,
            status=status,
            error=error,
            telegram_message_ids=message_ids,
            created_at=now,
            sent_at=now if status == "sent" else None,
            content_id=content_id,
            **columns,
        )
        self.rows[(learner_id, day)] = record
        return record


class FakeContents:
    """Shared lesson content with the same claim rules as `LessonContentRepo`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int, str], LessonContentRecord] = {}

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

    async def finish(
        self, content_id: int, content: Lesson | TopicContent, *, run: RunReport | None = None
    ) -> LessonContentRecord:
        kind = "topic" if isinstance(content, TopicContent) else "lesson"
        return self._update(
            content_id,
            status="ready",
            payload=content.model_dump(mode="json"),
            payload_kind=kind,
            error=None,
            generated_at=datetime.now(UTC),
            run=run,
        )

    async def fail(self, content_id: int, reason: str, *, run: RunReport | None = None) -> LessonContentRecord:
        return self._update(content_id, status="failed", error=reason, run=run)

    async def get(self, content_id: int) -> LessonContentRecord | None:
        return next((row for row in self.rows.values() if row.id == content_id), None)

    async def ready_contents(self, content_key: str) -> list[LessonContentRecord]:
        return [row for (_, _, _, key), row in self.rows.items() if key == content_key and row.status == "ready"]

    async def ready_topics(self, cefr: Cefr, curriculum_version: int, content_key: str) -> set[str]:
        return {
            topic_id
            for (topic_id, level, version, key), row in self.rows.items()
            if (level, version, key) == (cefr, curriculum_version, content_key) and row.status == "ready"
        }

    async def unavailable_topics(
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

    async def get_by_key(self, key: str) -> ItemRecord | None:
        return self.by_key.get(key)

    async def get_or_create(self, draft: ItemDraft, *, source_lesson_id: int | None = None) -> tuple[ItemRecord, bool]:
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

    async def ids_for_keys(self, keys: list[str]) -> dict[str, int]:
        return {key: self.by_key[key].id for key in keys if key in self.by_key}


class FakeCards:
    """Cards without FSRS: a review bumps the counter and pushes `due_at` one day out."""

    def __init__(self, items: FakeItems | None = None) -> None:
        self.by_key: dict[tuple[int, int], CardRecord] = {}
        # (learner id, item id, rating, source_ref, reviewed_at)
        self.reviews: list[tuple[int, int, int, str, datetime]] = []
        self._items = items  # to resolve a card's item for memory

    async def get(self, learner_id: int, item_id: int) -> CardRecord | None:
        return self.by_key.get((learner_id, item_id))

    async def due(self, learner_id: int, now: datetime, *, limit: int = 20) -> list[CardRecord]:
        due = [c for c in self.by_key.values() if c.learner_id == learner_id and c.due_at <= now]
        return sorted(due, key=lambda c: c.due_at)[:limit]

    async def for_items(self, learner_id: int, item_ids: list[int]) -> list[CardRecord]:
        return [c for c in self.by_key.values() if c.learner_id == learner_id and c.item_id in set(item_ids)]

    async def memory(self, learner_id: int, now: datetime) -> list[CardMemory]:
        """Like the repository's, with a stand-in recall probability: lower the more overdue, 0.9 when not due."""
        if self._items is None:
            return []
        id_to_item = {item.id: item for item in self._items.by_key.values()}
        result: list[CardMemory] = []
        for card in self.by_key.values():
            item = id_to_item.get(card.item_id)
            if card.learner_id != learner_id or item is None:
                continue
            overdue_days = (now - card.due_at).total_seconds() / 86400
            recall = 0.9 if overdue_days < 0 else max(0.0, 0.8 - 0.1 * overdue_days)
            result.append(
                CardMemory(
                    item_id=item.id,
                    item_key=item.key,
                    topic_id=item.topic_id,
                    due_at=card.due_at,
                    retrievability=recall,
                )
            )
        return result

    async def item_accuracy(self, learner_id: int, item_ids: list[int]) -> dict[int, ItemAccuracy]:
        wanted = set(item_ids)
        by_item: dict[int, list[tuple[int, datetime]]] = {}
        for lid, item_id, rating, _ref, at in self.reviews:
            if lid == learner_id and item_id in wanted:
                by_item.setdefault(item_id, []).append((rating, at))
        result: dict[int, ItemAccuracy] = {}
        for item_id, entries in by_item.items():
            result[item_id] = ItemAccuracy(
                item_id=item_id,
                checks=len(entries),
                correct=sum(rating >= 3 for rating, _ in entries),
                days=len({at.astimezone(UTC).date() for _, at in entries}),
            )
        return result

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
        if any(entry[3] == source_ref for entry in self.reviews):
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
        self.reviews.append((learner_id, item_id, rating, source_ref, at))
        return card


class FakeProfiles:
    def __init__(self, learners: FakeLearners) -> None:
        self._learners = learners
        self.versions: dict[int, list[LearnerProfileRecord]] = {}
        self.next_inputs: ProfileInputs | None = None  # what `inputs` returns next, when a test sets it

    async def latest(self, learner_id: int) -> LearnerProfileRecord | None:
        versions = self.versions.get(learner_id)
        return versions[-1] if versions else None

    async def append(self, learner_id: int, profile: LearnerProfile) -> LearnerProfileRecord:
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

    async def inputs(self, learner_id: int, now: datetime, *, window_days: int) -> ProfileInputs:
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

    async def delivery(self, lesson_id: int, kind: PracticeKind) -> PracticeDeliveryRecord | None:
        return next((d for d in self.deliveries.values() if d.lesson_id == lesson_id and d.kind == kind), None)

    async def claim_delivery(
        self, lesson_id: int, kind: PracticeKind, item_count: int, *, payload: dict[str, Any] | None = None
    ) -> PracticeDeliveryRecord | None:
        if await self.delivery(lesson_id, kind) is not None:
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
            payload=payload,
        )
        self.deliveries[record.id] = record
        return record

    async def mark_sent(self, delivery_id: int, message_ids: list[int]) -> PracticeDeliveryRecord:
        record = self.deliveries[delivery_id].model_copy(
            update={"sent_at": datetime.now(UTC), "telegram_message_ids": message_ids}
        )
        self.deliveries[delivery_id] = record
        return record

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

    async def record_poll_answer(
        self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]
    ) -> QuizResult | None:
        poll = self.polls.get(telegram_poll_id)
        learner = self._learners.by_chat.get(telegram_chat_id)
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

    async def asked(self, learner_id: int, since: datetime) -> AskedHistory:
        learner_of = {row.id: row.learner_id for row in self._lessons.rows.values()}
        history = AskedHistory()
        for delivery in self.deliveries.values():
            if learner_of.get(delivery.lesson_id) != learner_id or delivery.claimed_at < since or not delivery.payload:
                continue
            block = PracticeBlock.model_validate(delivery.payload)
            for question in block.questions:
                history.questions.setdefault(question.item_key, set()).add(question.question.question)
            for sentence in block.sentences:
                history.sentences.setdefault(sentence.item_key, set()).add(sentence.task.ru)
        return history

    async def wrong_quiz_items(self, lesson_id: int) -> set[int]:
        quiz_ids = {d.id for d in self.deliveries.values() if d.lesson_id == lesson_id and d.kind == "quiz"}
        return {
            poll.item_id
            for poll in self.polls.values()
            if poll.delivery_id in quiz_ids
            and poll.item_id is not None
            and any(not correct for (poll_id, _), correct in self.poll_answers.items() if poll_id == poll.id)
        }

    async def errors_for_items(self, learner_id: int, item_ids: list[int]) -> list[ErrorRecord]:
        return []  # the fake keeps errors without their item

    async def open_translation(self, telegram_chat_id: str) -> OpenTranslationTask | None:
        learner = self._learners.by_chat.get(telegram_chat_id)
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
        if (answer is not None and answer.status != "failed") or lessons[delivery.lesson_id].payload is None:
            return None
        return OpenTranslationTask(learner_id=learner.id, delivery=delivery)

    async def start_translation_answer(
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

    async def set_progress_message(self, answer_id: int, message_id: int) -> None:
        self._update_answer(answer_id, progress_message_id=message_id)

    async def translation_for_grading(self, answer_id: int) -> GradingTask | None:
        answer = self.translation_answers.get(answer_id)
        if answer is None:
            return None
        delivery = self.deliveries[answer.delivery_id]
        record = next(r for r in self._lessons.rows.values() if r.id == delivery.lesson_id)
        learner = next(r for r in self._learners.by_chat.values() if r.id == answer.learner_id)
        grading_input = _fake_grading_input(record, delivery)
        if grading_input is None:
            return None
        return GradingTask(answer=answer, input=grading_input, telegram_chat_id=learner.telegram_chat_id)

    async def begin_grading(self, answer_id: int, now: datetime) -> GradingTask | None:
        answer = self.translation_answers.get(answer_id)
        if answer is None or answer.status != "pending":
            return None
        self._update_answer(answer_id, grading_attempts=answer.grading_attempts + 1, grading_started_at=now)
        return await self.translation_for_grading(answer_id)

    async def stale_gradings(self, started_before: datetime, *, limit: int = 200) -> list[tuple[int, int]]:
        return [
            (a.id, a.grading_attempts)
            for a in sorted(self.translation_answers.values(), key=lambda a: a.created_at)
            if a.status == "pending" and (a.grading_started_at or a.created_at) < started_before
        ][:limit]

    async def delivered_kinds(self, lesson_ids: list[int]) -> dict[int, set[PracticeKind]]:
        kinds: dict[int, set[PracticeKind]] = {}
        for delivery in self.deliveries.values():
            if delivery.lesson_id in lesson_ids:
                kinds.setdefault(delivery.lesson_id, set()).add(delivery.kind)
        return kinds

    async def finish_translation_answer(self, answer_id: int, grade: TranslationGrade) -> TranslationAnswerRecord:
        record = self._update_answer(answer_id, status="graded", grade=grade, graded_at=datetime.now(UTC))
        self.errors.extend(("text", s.learner_text, s.corrected) for s in grade.sentences if s.verdict == "fixed")
        return record

    async def fail_translation_answer(self, answer_id: int, reason: str) -> None:
        self._update_answer(answer_id, status="failed", error=reason)

    async def dispute(self, answer_id: int) -> bool:
        answer = self.translation_answers.get(answer_id)
        if answer is None or answer.status != "graded":
            return False
        self._update_answer(answer_id, disputed=True)
        return True

    async def for_arbitration(self, answer_id: int) -> GradingTask | None:
        return None  # arbitration runs against PostgreSQL (tests/test_arbiter.py)

    async def overturn(
        self, answer_id: int, arbiter: TranslationGrade, sentences: list[int], now: datetime
    ) -> list[int]:
        return []

    def _update_answer(self, answer_id: int, **changes: object) -> TranslationAnswerRecord:
        record = self.translation_answers[answer_id].model_copy(update=changes)
        self.translation_answers[answer_id] = record
        return record


class FakeEvidence:
    """`EvidenceStore` over records the test adds."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    async def for_items(self, learner_id: int, item_ids: list[int], *, limit: int) -> list[EvidenceRecord]:
        mine = [r for r in self.records if r.learner_id == learner_id and r.item_id in item_ids]
        return sorted(mine, key=lambda r: (r.occurred_at, r.id))[-limit:]

    async def question_keys(self, learner_id: int) -> set[str]:
        return {
            str(r.payload["question_key"])
            for r in self.records
            if r.learner_id == learner_id and "question_key" in r.payload
        }

    async def count_since(self, learner_id: int, kind: str, since: datetime) -> int:
        return sum(1 for r in self.records if r.learner_id == learner_id and r.kind == kind and r.occurred_at >= since)

    async def record(
        self,
        learner_id: int,
        kind: str,
        *,
        source_ref: str,
        topic_id: str | None,
        payload: dict[str, Any],
        at: datetime,
    ) -> None:
        if any(r.source_ref == source_ref for r in self.records):
            return
        self.records.append(
            EvidenceRecord.model_validate(
                {
                    "id": len(self.records) + 1,
                    "learner_id": learner_id,
                    "kind": kind,
                    "source_ref": source_ref,
                    "topic_id": topic_id,
                    "item_id": None,
                    "error_id": None,
                    "payload": payload,
                    "occurred_at": at,
                    "cancelled_at": None,
                }
            )
        )

    async def topic_answers(self, learner_id: int, topic_ids: list[str]) -> dict[str, tuple[int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for r in self.records:
            if r.learner_id == learner_id and r.topic_id in topic_ids:
                total, right = counts.get(r.topic_id, (0, 0))
                counts[r.topic_id] = (total + 1, right + int(bool(r.payload.get("is_correct"))))
        return counts


class FakeOrders:
    """`OrderStore` in memory, one order per (brief topic, level)."""

    def __init__(self) -> None:
        self.orders: dict[tuple[str, str], ContentOrder] = {}

    async def order(self, brief: ContentBrief, cefr: Cefr) -> ContentOrder:
        key = (brief.topic_id, cefr)
        if key not in self.orders:
            self.orders[key] = ContentOrder(
                id=len(self.orders) + 1,
                topic_id=brief.topic_id,
                cefr=cefr,
                brief=brief,
                status="ordered",
                content_id=None,
                created_at=datetime.now(UTC),
                finished_at=None,
            )
        return self.orders[key]

    async def by_topic(self, topic_id: str, cefr: Cefr) -> ContentOrder | None:
        return self.orders.get((topic_id, cefr))

    async def open_orders(self, limit: int) -> list[ContentOrder]:
        return [order for order in self.orders.values() if order.status == "ordered"][:limit]

    async def finish(self, order_id: int, status: OrderStatus, content_id: int | None, now: datetime) -> None:
        for key, order in self.orders.items():
            if order.id == order_id:
                self.orders[key] = order.model_copy(
                    update={"status": status, "content_id": content_id, "finished_at": now}
                )


class FakeExercises:
    def __init__(self) -> None:
        self.bank: dict[str, BankExercise] = {}
        self.priors: dict[str, float] = {}

    async def save(self, exercises: list[BankExercise], priors: dict[str, float]) -> None:
        for exercise in exercises:
            self.bank.setdefault(exercise.question_key, exercise)
        self.priors.update(priors)

    async def get(self, keys: list[str]) -> list[BankExercise]:
        return [self.bank[key] for key in keys if key in self.bank]


class FakeExams:
    """`ExamStore` in memory: polls map to exams; answers are kept, evidence is not written."""

    def __init__(self, learners: FakeLearners) -> None:
        self.learners = learners
        self.exams: dict[int, ExamRecord] = {}
        self.polls: dict[str, tuple[int, str, bool]] = {}

    async def create(
        self,
        learner_id: int,
        day: date,
        topic_ids: list[str],
        ability: float,
        pool: list[PoolQuestion],
        kind: ExamKind = "exam",
    ) -> ExamRecord | None:
        if any(e.learner_id == learner_id and e.plan_date == day and e.kind == kind for e in self.exams.values()):
            return None
        exam = ExamRecord(
            id=len(self.exams) + 1,
            learner_id=learner_id,
            plan_date=day,
            kind=kind,
            state="running",
            topic_ids=topic_ids,
            ability_start=ability,
            ability=ability,
            pool=pool,
            answers=[],
            report=None,
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        self.exams[exam.id] = exam
        return exam

    async def get(self, exam_id: int) -> ExamRecord | None:
        return self.exams.get(exam_id)

    async def add_poll(self, exam_id: int, telegram_poll_id: str, question_key: str) -> None:
        self.polls[telegram_poll_id] = (exam_id, question_key, False)

    async def has_open_poll(self, exam_id: int) -> bool:
        return any(e == exam_id and not answered for e, _, answered in self.polls.values())

    async def record_answer(self, telegram_poll_id: str, telegram_chat_id: str, option_ids: list[int]) -> int | None:
        if telegram_poll_id not in self.polls:
            return None
        exam_id, key, answered = self.polls[telegram_poll_id]
        exam = self.exams[exam_id]
        if answered or exam.state != "running":
            return None
        asked = next(q for q in exam.pool if q.question_key == key)
        answer = ExamAnswer(
            question_key=key, topic_id=asked.topic_id, correct=option_ids == [asked.question.correct_option]
        )
        self.polls[telegram_poll_id] = (exam_id, key, True)
        self.exams[exam_id] = exam.model_copy(update={"answers": [*exam.answers, answer]})
        return exam_id

    async def finish(self, exam_id: int, report: ExamReport, now: datetime) -> None:
        self.exams[exam_id] = self.exams[exam_id].model_copy(
            update={"state": "finished", "report": report, "finished_at": now}
        )

    async def last_exam_on(self, learner_id: int) -> date | None:
        days = [e.plan_date for e in self.exams.values() if e.learner_id == learner_id]
        return max(days) if days else None


class FakeKnowledge:
    """`KnowledgeStore` with topics set as known by the test."""

    def __init__(self) -> None:
        self.known: dict[int, set[str]] = {}
        self.stats: dict[str, QuestionStat] = {}
        self.abilities: dict[int, LearnerAbility] = {}
        self.marks: dict[int, dict[str, str]] = {}

    async def known_topics(self, learner_id: int, now: datetime) -> set[str]:
        return set(self.known.get(learner_id, set()))

    async def questions(self, keys: list[str]) -> dict[str, QuestionStat]:
        return {key: self.stats[key] for key in keys if key in self.stats}

    async def ability(self, learner_id: int) -> LearnerAbility | None:
        return self.abilities.get(learner_id)

    async def mark_topics(
        self, learner_id: int, topic_ids: list[str], mark: str, *, source_ref: str, at: datetime
    ) -> None:
        self.marks.setdefault(learner_id, {}).update(dict.fromkeys(topic_ids, mark))


class FakePlans:
    """`PlanStore` in memory, with the same commit-once rule."""

    def __init__(self) -> None:
        self.records: dict[tuple[int, date], DayPlanRecord] = {}

    async def get(self, learner_id: int, day: date) -> DayPlanRecord | None:
        return self.records.get((learner_id, day))

    async def start(self, learner_id: int, day: date) -> bool:
        if (learner_id, day) in self.records:
            return False
        self.records[(learner_id, day)] = DayPlanRecord(
            id=len(self.records) + 1,
            learner_id=learner_id,
            plan_date=day,
            state="running",
            plan=None,
            trajectory=None,
            created_at=datetime.now(UTC),
            committed_at=None,
        )
        return True

    async def commit(
        self, plan: DayPlan, *, trajectory: list[dict[str, Any]] | None = None, now: datetime
    ) -> DayPlanRecord:
        key = (plan.learner_id, plan.plan_date)
        current = self.records.get(key)
        if current is not None and current.state in ("committed", "fallback"):
            return current
        self.records[key] = DayPlanRecord(
            id=len(self.records) + 1,
            learner_id=plan.learner_id,
            plan_date=plan.plan_date,
            state="committed" if plan.source == "agent" else "fallback",
            plan=plan,
            trajectory=trajectory,
            created_at=now,
            committed_at=now,
        )
        return self.records[key]


def fake_stores() -> tuple[Stores, FakeLearners, FakeLessons]:
    learners, lessons = FakeLearners(), FakeLessons()
    items = FakeItems()
    stores = Stores(
        learners=learners,
        lessons=lessons,
        practice=FakePractice(learners, lessons),
        items=items,
        cards=FakeCards(items),
        profiles=FakeProfiles(learners),
        contents=FakeContents(),
        plans=FakePlans(),
        knowledge=FakeKnowledge(),
        evidence=FakeEvidence(),
        orders=FakeOrders(),
        exercises=FakeExercises(),
        exams=FakeExams(learners),
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


class ScriptedLLM:
    """A `ChatLLM` that answers with the scripted turns in order and records every request."""

    model = "claude-haiku-5-5"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []  # {"messages", "tools", "tool_choice"} per request

    @property
    def transcripts(self) -> list[list[dict[str, Any]]]:
        return [call["messages"] for call in self.calls]

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any = "auto"
    ) -> AssistantTurn:
        self.calls.append({"messages": list(messages), "tools": tools, "tool_choice": tool_choice})
        if not self.turns:
            raise AssertionError("LLM asked for more turns than scripted")
        return self.turns.pop(0)


class FakeTools:
    """`ResearchTools` over in-memory sitemaps and pages."""

    def __init__(self, urls: dict[str, list[str]], pages: dict[str, str]) -> None:
        self.urls = urls
        self.pages = pages
        self.fetched: list[str] = []

    async def search_site(self, site: Site, keywords: list[str], limit: int = 5) -> list[SearchHit]:
        return rank_urls(self.urls.get(site.name, []), keywords, limit)

    async def fetch_page(self, url: str) -> PageContent:
        self.fetched.append(url)
        return PageContent(url=url, title=f"Title of {url}", text=self.pages[url], truncated=False)
