"""The learner profile: a compact, versioned picture of one learner that the agents read.

Every number here is computed by code from the stored answers, errors and cards
(see `english_agent.profile`); the model never gets to rewrite them. `notes_ru` is the one
field reserved for the Reflector's own words.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from english_agent.models.cefr import Cefr
from english_agent.versions import Provenance

PROFILE_WINDOW_DAYS = 7
MAX_WEAK_POINTS = 10
MAX_ERROR_PATTERNS = 10


class WeakPoint(BaseModel):
    """An item the learner got wrong inside the window, most errors first."""

    item_id: int
    item_key: str
    topic_id: str
    errors: int = Field(ge=1)
    last_error_at: datetime


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
    notes_ru: str = ""  # written by the Reflector; carried over unchanged by code
    # Code-owned: how this version was produced.
    provenance: Provenance | None = None

    def same_as(self, other: "LearnerProfile") -> bool:
        """True when nothing but provenance differs: no new version is worth storing then."""
        return self.model_dump(exclude={"provenance"}) == other.model_dump(exclude={"provenance"})
