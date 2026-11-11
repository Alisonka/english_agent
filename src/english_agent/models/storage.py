"""What the storage layer hands out: pydantic copies of database rows.

Repositories build these with `Record.model_validate(orm_row)` (`from_attributes`),
so SQLAlchemy objects never leave `english_agent.storage`.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson, Source
from english_agent.models.practice import QuizQuestion, TranslationGrade
from english_agent.models.profile import LearnerProfile

LessonStatus = Literal["sent", "failed"]
ErrorKind = Literal["quiz", "text"]
PracticeKind = Literal["quiz", "translation"]
TranslationAnswerStatus = Literal["pending", "graded", "failed"]


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


CORE_SLUG = "core"


def core_item_key(topic_id: str) -> str:
    """Key of the item that stands for the topic as a whole: `<topic_id>:core`."""
    return f"{topic_id}:{CORE_SLUG}"


class ItemDraft(BaseModel):
    """What a new item is made of; `ItemStore.get_or_create` turns it into an `ItemRecord`."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$", max_length=200)  # "<topic_id>:<slug>"
    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    cefr: Cefr
    form: str = Field(min_length=1)
    function_ru: str = Field(min_length=1)
    explanation_ru: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)


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
    """One learner's memory of one item, as FSRS tracks it; `due_at` is when to review it next."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    item_id: int
    fsrs_state: dict[str, Any]
    due_at: datetime
    last_reviewed_at: datetime | None
    reviews: int
    created_at: datetime


class ReviewLogRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_id: int
    rating: int = Field(ge=1, le=4)  # FSRS: 1 again, 2 hard, 3 good, 4 easy
    reviewed_at: datetime
    source: str  # "quiz" | "translation"
    source_ref: str  # the event that was rated, e.g. "quiz_answer:12"; unique, so a replay is a no-op
    payload: dict[str, Any] | None


class PracticeDeliveryRecord(BaseModel):
    """The quiz or the translation task of one lesson; `sent_at` is None while it is being sent."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    lesson_id: int
    kind: PracticeKind
    item_count: int
    claimed_at: datetime
    sent_at: datetime | None
    telegram_message_ids: list[int]
    completed_at: datetime | None


class QuizPollRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    delivery_id: int
    position: int
    telegram_poll_id: str
    telegram_message_id: int
    item_id: int | None  # the item the question checks; None for polls sent before items existed
    question: QuizQuestion  # options in the order they were sent
    created_at: datetime


class QuizResult(BaseModel):
    """What answering one poll changed; `results` is filled once every poll of the quiz is answered."""

    learner_id: int
    is_correct: bool
    completed: bool
    results: list[tuple[QuizQuestion, bool]] = Field(default_factory=list)


class TranslationAnswerRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    delivery_id: int
    learner_id: int
    answer_text: str
    status: TranslationAnswerStatus
    grade: TranslationGrade | None
    error: str | None
    progress_message_id: int | None
    disputed: bool
    created_at: datetime
    graded_at: datetime | None


class OpenTranslationTask(BaseModel):
    """A sent translation task that waits for the learner's answer."""

    learner_id: int
    delivery: PracticeDeliveryRecord
    lesson: Lesson


class LearnerProfileRecord(BaseModel):
    """One version of a learner's profile; versions are only ever appended."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    version: int
    schema_version: int
    profile: LearnerProfile
    created_at: datetime


class ProfileInputs(BaseModel):
    """What `english_agent.profile.build_profile` needs: one learner's last `window_days` of activity."""

    cefr: Cefr
    window_days: int
    lessons_sent: int = 0
    errors: list[ErrorRecord] = Field(default_factory=list)  # inside the window, any order
    items: dict[int, ItemRecord] = Field(default_factory=dict)  # the items those errors point to
    quiz_answers: int = 0
    quiz_correct: int = 0
    translation_grades: list[TranslationGrade] = Field(default_factory=list)  # graded inside the window
    cards: int = 0
    due_cards: int = 0
    notes_ru: str = ""  # carried over from the latest stored version
