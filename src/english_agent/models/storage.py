"""What the storage layer hands out: pydantic copies of database rows.

Repositories build these with `Record.model_validate(orm_row)` (`from_attributes`),
so SQLAlchemy objects never leave `english_agent.storage`.
"""

from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Lesson, Source
from english_agent.models.practice import QuizQuestion, TranslationGrade, TranslationTask
from english_agent.models.profile import LearnerProfile, Reflection
from english_agent.models.run import RunReport
from english_agent.models.schedule import LearnerSchedule
from english_agent.models.topic import LessonStep, TopicContent
from english_agent.versions import Provenance

LessonStatus = Literal["pending", "sending", "sent", "failed", "skipped"]
LessonClaim = Literal["claimed", "resume", "busy", "done"]
ContentStatus = Literal["generating", "ready", "failed"]
ClaimOutcome = Literal["ready", "claimed", "busy", "exhausted"]
ErrorKind = Literal["quiz", "text"]
PracticeKind = Literal["quiz", "translation", "summary"]
TranslationAnswerStatus = Literal["pending", "graded", "failed"]
PayloadKind = Literal["lesson", "step"]
StepVariant = Literal["main", "alt"]


class LearnerRecord(BaseModel):
    """A learner and their schedule. Defaults mirror the column defaults of the `learners` table."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_chat_id: str
    cefr: Cefr
    created_at: datetime
    updated_at: datetime
    timezone: str = "Europe/Moscow"
    lesson_send_time: time = time(9, 0)
    translation_send_time: time = time(18, 0)
    quiz_delay_minutes: int = 60
    curriculum_started_on: date | None = None  # None until /start: no lessons before that
    is_active: bool = True  # False once the learner blocked the bot or finished the course
    paused_at: datetime | None = None  # /pause: no lessons and no practice until /resume
    deactivated_reason: str | None = None  # "blocked" | "course_finished"

    @property
    def schedule(self) -> LearnerSchedule:
        return LearnerSchedule(
            timezone=self.timezone,
            lesson_send_time=self.lesson_send_time,
            translation_send_time=self.translation_send_time,
            quiz_delay_minutes=self.quiz_delay_minutes,
        )

    @property
    def receives_lessons(self) -> bool:
        return self.is_active and self.paused_at is None and self.curriculum_started_on is not None


class LessonRecord(BaseModel):
    """One learner's lesson for one day; `payload` is None when the run failed.

    The `payload` JSONB holds a whole `Lesson` (legacy days, `payload_kind="lesson"`) or one
    `LessonStep` (`payload_kind="step"`); read it through the `lesson` / `step` properties.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    learner_id: int
    lesson_date: date
    topic_id: str
    cefr: Cefr
    curriculum_version: int | None
    schema_version: int  # the schema version the payload was written with
    status: LessonStatus
    payload: dict[str, Any] | None = None
    error: str | None = None
    telegram_message_ids: list[int] = Field(default_factory=list)
    created_at: datetime
    sent_at: datetime | None = None
    content_id: int | None = None  # the shared `LessonContentRecord` that was sent; None for older lessons
    claimed_at: datetime | None = None  # a worker is delivering it since then (None: nobody holds it)
    attempts: int = 0  # delivery attempts so far
    # Which step of the topic this day is. Legacy rows are step 1 of 1, slug "core", a main variant.
    payload_kind: PayloadKind = "lesson"
    step_index: int = 1
    step_count: int = 1
    step_slug: str = "core"
    variant: StepVariant = "main"

    @property
    def lesson(self) -> "Lesson | None":
        """The legacy whole-topic lesson, when this day holds one."""
        if self.payload_kind != "lesson" or self.payload is None:
            return None
        return Lesson.model_validate(self.payload)

    @property
    def step(self) -> "LessonStep | None":
        """The step this day teaches, when it holds one."""
        if self.payload_kind != "step" or self.payload is None:
            return None
        return LessonStep.model_validate(self.payload)

    @property
    def item_key(self) -> str:
        """The learnable item this day teaches: the topic core item, or the step item."""
        if self.payload_kind == "lesson":
            return core_item_key(self.topic_id)
        return f"{self.topic_id}:{self.step_slug}"


class LessonState(BaseModel):
    """A day's row without the payload: what the planner needs to decide whether to (re)run it."""

    lesson_id: int
    status: LessonStatus
    attempts: int
    claimed_at: datetime | None
    sent_at: datetime | None


class LessonContentRecord(BaseModel):
    """A lesson written once for every learner of a topic and level, by one generation recipe.

    `content_key` names the recipe (schema version, prompt fingerprints, models): changing any of
    them makes new content instead of reusing lessons an older recipe wrote.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: str
    cefr: Cefr
    curriculum_version: int  # 0 for lessons backfilled from before shared content
    content_key: str
    status: ContentStatus
    schema_version: int
    payload_kind: Literal["lesson", "topic"] = "lesson"  # "topic" holds a whole TopicContent
    payload: dict[str, Any] | None = None
    error: str | None
    attempts: int
    claimed_at: datetime | None
    claimed_by: str | None
    generated_at: datetime | None
    created_at: datetime
    run: RunReport | None = None  # how the last generation run went; None for content written before reports

    @property
    def lesson(self) -> "Lesson | None":
        if self.payload_kind != "lesson" or self.payload is None:
            return None
        return Lesson.model_validate(self.payload)

    @property
    def topic(self) -> "TopicContent | None":
        if self.payload_kind != "topic" or self.payload is None:
            return None
        return TopicContent.model_validate(self.payload)


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
    payload: dict[str, Any] | None = None  # the assembled PracticeBlock (typed in the practice group)


PracticeSource = Literal["today", "previous", "review"]
# Why the Planner chose an item: today's step, the topic's least-recalled earlier step, an item due
# for review, one the learner keeps getting wrong, one missed in today's quiz, the least recalled,
# or one of the Reflector's focus points (due or weak).
PlanReason = Literal["today", "previous", "due", "weak", "wrong_today", "least_recalled", "focus"]


class AssembledQuestion(BaseModel):
    """One quiz question chosen for a day, tagged with the item it checks and where it came from."""

    question: QuizQuestion
    item_id: int | None
    item_key: str
    source: PracticeSource
    reason: PlanReason | None = None  # None for blocks assembled before the Planner


class AssembledSentence(BaseModel):
    """One translation sentence chosen for a day, with the context the grader needs."""

    task: TranslationTask
    item_id: int | None
    item_key: str
    title_en: str
    key_idea_ru: str | None = None
    form: str | None = None
    source: PracticeSource
    reason: PlanReason | None = None  # None for blocks assembled before the Planner


class PracticeBlock(BaseModel):
    """The day's assembled practice set, stored in `practice_deliveries.payload`.

    A quiz block carries `questions`, a translation block carries `sentences`; the other list is empty.
    """

    kind: Literal["quiz", "translation"]
    questions: list[AssembledQuestion] = Field(default_factory=list)
    sentences: list[AssembledSentence] = Field(default_factory=list)
    # Code-owned: the Planner that chose the items, and the profile version whose focus it used.
    provenance: Provenance | None = None
    profile_version: int | None = None


class SentStep(BaseModel):
    """A sent step of a topic, for progression and re-teach decisions (no payload)."""

    model_config = ConfigDict(from_attributes=True)

    lesson_id: int
    topic_id: str
    lesson_date: date
    step_index: int
    step_count: int
    step_slug: str
    variant: StepVariant
    content_id: int | None = None

    @property
    def item_key(self) -> str:
        return f"{self.topic_id}:{self.step_slug}"


class CardMemory(BaseModel):
    """One card as the Planner sees it: the item, when FSRS wants it reviewed, how likely it is recalled now."""

    item_id: int
    item_key: str
    topic_id: str
    due_at: datetime
    retrievability: float = Field(ge=0, le=1)


class AskedHistory(BaseModel):
    """What the learner was already asked recently, per item key: quiz question texts and Russian sentences."""

    questions: dict[str, set[str]] = Field(default_factory=dict)
    sentences: dict[str, set[str]] = Field(default_factory=dict)


class ItemAccuracy(BaseModel):
    """How a learner has done on one item across all its reviews: counts and distinct review days."""

    item_id: int
    checks: int
    correct: int
    days: int  # distinct calendar days the item was reviewed on (UTC)

    @property
    def ratio(self) -> float | None:
        return self.correct / self.checks if self.checks else None


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
    grading_attempts: int = 0
    grading_started_at: datetime | None = None


class OpenTranslationTask(BaseModel):
    """A sent translation task that waits for the learner's answer."""

    learner_id: int
    delivery: PracticeDeliveryRecord


class GradingSentence(BaseModel):
    """One sentence to grade, with the step context that tells the grader what it checks."""

    n: int = Field(ge=1)
    ru: str
    reference_en: str
    focus_ru: str
    key_idea_ru: str | None = None
    form: str | None = None


class GradingInput(BaseModel):
    """What the grader sees: the day's sentences and their per-step context (not a whole lesson)."""

    title_en: str
    title_ru: str
    cefr: Cefr
    topic_id: str
    sentences: list[GradingSentence]
    learner: Reflection | None = None  # the Reflector's current reading of the learner, for the explanations


class GradingTask(BaseModel):
    """What grading one translation answer needs: the answer, its input and where to reply."""

    answer: TranslationAnswerRecord
    input: GradingInput
    telegram_chat_id: str


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
    reflection: Reflection | None = None  # carried over from the latest stored version, or a new one
