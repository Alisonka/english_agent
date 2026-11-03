"""In-memory stand-ins for the storage repositories, for tests that need no database."""

from __future__ import annotations

from datetime import UTC, date, datetime

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson
from english_agent.models.storage import LearnerRecord, LessonRecord
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


def fake_stores() -> tuple[Stores, FakeLearners, FakeLessons]:
    learners, lessons = FakeLearners(), FakeLessons()
    return Stores(learners=learners, lessons=lessons), learners, lessons
