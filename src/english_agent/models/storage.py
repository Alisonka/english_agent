"""What the storage layer hands out: pydantic copies of database rows.

Repositories build these with `Record.model_validate(orm_row)` (`from_attributes`),
so SQLAlchemy objects never leave `english_agent.storage`.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson, Source

LessonStatus = Literal["sent", "failed"]
ErrorKind = Literal["quiz", "text"]


class LearnerRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_chat_id: str
    cefr: Cefr
    created_at: datetime
    updated_at: datetime


class LessonRecord(BaseModel):
    """One learner's lesson for one day; `lesson` is None when the run failed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    lesson_date: date
    topic_id: str
    cefr: Cefr
    curriculum_version: int | None
    schema_version: int  # SCHEMA_VERSIONS["lesson"] the payload was written with
    status: LessonStatus
    lesson: Lesson | None
    error: str | None
    telegram_message_ids: list[int]
    created_at: datetime
    sent_at: datetime | None


class ItemRecord(BaseModel):
    """A learnable grammar item (a form and what it is for), the unit FSRS schedules."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str  # "<topic_id>:<slug>"
    topic_id: str
    cefr: Cefr
    form: str
    function_ru: str
    explanation_ru: str
    sources: list[Source]
    version: int
    source_lesson_id: int | None
    created_at: datetime


class ErrorRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    item_id: int | None
    kind: ErrorKind
    original: str
    corrected: str
    error_type: str | None
    explanation_ru: str | None
    created_at: datetime


class CardRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    item_id: int
    fsrs_state: dict[str, Any]
    due_at: datetime
    created_at: datetime


class ReviewLogRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_id: int
    rating: int = Field(ge=1, le=4)  # FSRS: 1 again, 2 hard, 3 good, 4 easy
    reviewed_at: datetime
    source: str
    payload: dict[str, Any] | None
