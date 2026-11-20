"""The learner profile: a compact, versioned picture of one learner that the agents read.

Every number here is computed by code from the stored answers, errors and cards
(see `english_agent.profile`); the model never gets to rewrite them. `reflection` is the one
part written by a model: the Reflector's reading of the learner's recent errors (`reflector.py`),
each focus point tied to the errors it rests on. Consumers use it only while it is current.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from english_agent.models.cefr import Cefr
from english_agent.versions import Provenance

PROFILE_WINDOW_DAYS = 7
REFLECTION_TTL = timedelta(days=14)  # an older reflection no longer steers practice or grading
MAX_FOCUS_POINTS = 3
MAX_WEAK_POINTS = 10
MAX_ERROR_PATTERNS = 10


class WeakPoint(BaseModel):
    """An item the learner got wrong inside the window, most errors first."""

    item_id: int
    item_key: str
    topic_id: str
    errors: int = Field(ge=1)
    last_error_at: datetime


class TopicWeakPoint(BaseModel):
    """A step of a just-finished topic the learner struggled with, for the cheat sheet."""

    title_ru: str
    key_idea_ru: str
    accuracy: float | None = Field(default=None, ge=0, le=1)  # None when the step was never reviewed
    example: str | None = None  # the latest "<original> -> <corrected>" for the step, if any
    contrast_ru: str | None = None  # the Reflector's one-line hint, when one of its focus points is this step


class FocusPoint(BaseModel):
    """One recurring mistake the Reflector found, and the items and errors it rests on."""

    item_keys: Annotated[list[str], Field(min_length=1, max_length=3)]
    pattern_ru: str = Field(min_length=10, max_length=140)  # what keeps going wrong, in Russian
    contrast_ru: str = Field(min_length=10, max_length=160)  # one line that helps: the rule or the contrast
    error_type: str = Field(min_length=1, max_length=32)  # e.g. "article", "tense", "target_form"
    evidence_error_ids: Annotated[list[int], Field(min_length=1, max_length=5)]  # rows of `errors`
    # The Diagnostician's hypothesis (None in reflections written before it).
    cause: Literal["rule", "form", "l1_transfer", "other"] | None = None
    probe_ru: str | None = Field(default=None, min_length=10, max_length=200)  # a question that tells it apart
    intervention: Literal["remedial", "contrast", "reteach", "drill", "none"] | None = None


class Reflection(BaseModel):
    focus_points: list[FocusPoint] = Field(default_factory=list, max_length=MAX_FOCUS_POINTS)
    notes_ru: str = Field(default="", max_length=400)  # for the grader: what to stress, what already works
    # Code-owned.
    based_on_errors: int = Field(ge=0)  # how many errors the Reflector read
    until_error_id: int = Field(ge=0)  # the newest of them: later errors are "new" for the next reflection
    reflected_at: datetime
    provenance: Provenance | None = None

    def is_current(self, now: datetime) -> bool:
        return now - self.reflected_at < REFLECTION_TTL

    @property
    def focus_keys(self) -> frozenset[str]:
        return frozenset(key for point in self.focus_points for key in point.item_keys)


class ErrorPattern(BaseModel):
    """A kind of error (the grader's `error_type` or the quiz kind) and how often it happened."""

    error_type: str
    count: int = Field(ge=1)
    example: str  # latest "<original> -> <corrected>"


class ProfileStats(BaseModel):
    lessons_sent: int = Field(ge=0)
    quiz_answers: int = Field(ge=0)
    quiz_accuracy: float | None = Field(default=None, ge=0, le=1)  # None without answers in the window
    translation_sentences: int = Field(ge=0)
    translation_accuracy: float | None = Field(default=None, ge=0, le=1)
    cards: int = Field(ge=0)
    due_cards: int = Field(ge=0)


class LearnerProfile(BaseModel):
    cefr: Cefr
    window_days: int = PROFILE_WINDOW_DAYS
    weak_points: list[WeakPoint] = Field(default_factory=list, max_length=MAX_WEAK_POINTS)
    error_patterns: list[ErrorPattern] = Field(default_factory=list, max_length=MAX_ERROR_PATTERNS)
    stats: ProfileStats
    reflection: Reflection | None = None  # written by the Reflector; carried over unchanged by code
    # Code-owned: how this version was produced.
    provenance: Provenance | None = None

    def same_as(self, other: "LearnerProfile") -> bool:
        """True when nothing but provenance differs: no new version is worth storing then."""
        return self.model_dump(exclude={"provenance"}) == other.model_dump(exclude={"provenance"})
