"""Mastery estimates: the learner's ability and each question's difficulty, moved a small step per answer.

The same family as the Elo rating: P(correct) = logistic(ability - difficulty), where ability is the
learner's overall rating plus the mean offset of the topic's tag groups (tenses, articles, ...).
After an answer both sides move by K * (outcome - P): a right answer to a hard question raises the
ability more than one to an easy question. Content is shared, so a question's difficulty is
calibrated on every learner who saw it; until then it starts from the topic's level and the kind.

Every update returns its `EloDelta`; it is stored with the evidence, so an overturned grade can be
reversed exactly.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from functools import cache

from pydantic import BaseModel, ConfigDict, Field

from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import load_curriculum
from english_agent.paths import available_cefr_levels

QuestionKind = str  # "quiz" | "translation" (and later "exam", "probe")

LEVEL_DIFFICULTY: dict[Cefr, float] = {"A2": -1.0, "B1": 0.0, "B2": 1.0, "C1": 2.0}
KIND_OFFSET: dict[str, float] = {"quiz": 0.0, "translation": 0.5}
# A learner starts a little above their level: a question of that level is right about 6 times in 10.
START_ABOVE_LEVEL = 0.5
LEARNER_K = 0.4
TAG_K = 0.2
QUESTION_K = 0.4
QUESTION_K_HALF_LIFE = 20  # the question's step halves after this many answers: calibration settles


def question_key(item_key: str, text: str) -> str:
    """A stable id of one bank question or sentence: its item and a hash of its text."""
    return f"{item_key}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class TopicMeta:
    cefr: Cefr
    tags: tuple[str, ...]


@cache
def _topics() -> dict[str, TopicMeta]:
    return {
        topic.id: TopicMeta(curriculum.cefr, tuple(topic.tags))
        for cefr in CEFR_LEVELS
        if cefr in available_cefr_levels()
        for curriculum in [load_curriculum(cefr)]
        for topic in curriculum.topics
    }


def topic_meta(topic_id: str | None) -> TopicMeta | None:
    return _topics().get(topic_id) if topic_id is not None else None


def start_ability(cefr: Cefr) -> float:
    return LEVEL_DIFFICULTY[cefr] + START_ABOVE_LEVEL


def start_difficulty(topic: TopicMeta | None, kind: QuestionKind) -> float:
    level = LEVEL_DIFFICULTY[topic.cefr] if topic is not None else 0.0
    return level + KIND_OFFSET.get(kind, 0.0)


def probability(ability: float, tag_offsets: list[float], difficulty: float) -> float:
    offset = sum(tag_offsets) / len(tag_offsets) if tag_offsets else 0.0
    return 1 / (1 + math.exp(-(ability + offset - difficulty)))


class EloDelta(BaseModel):
    """How one answer moved the estimates (stored in the evidence payload); subtracting it undoes it."""

    model_config = ConfigDict(frozen=True)

    p: float  # the predicted chance of a right answer before it
    learner: float
    tags: dict[str, float] = Field(default_factory=dict)
    question: float = 0.0


def elo_step(
    ability: float, tag_offsets: dict[str, float], difficulty: float, question_answers: int, correct: bool
) -> EloDelta:
    """The move for one answer: the learner and their tag groups up on surprise success, the question down."""
    p = probability(ability, list(tag_offsets.values()), difficulty)
    surprise = (1.0 if correct else 0.0) - p
    question_k = QUESTION_K / (1 + question_answers / QUESTION_K_HALF_LIFE)
    return EloDelta(
        p=p,
        learner=LEARNER_K * surprise,
        tags=dict.fromkeys(tag_offsets, TAG_K * surprise),
        question=-question_k * surprise,
    )
