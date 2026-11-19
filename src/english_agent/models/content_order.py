"""Content the Mentor orders beyond the curriculum: a remedial mini-lesson or a contrast lesson.

Content stays shared: a remedial is written once per (topic, error type) and a contrast once per
pair, then reused by every learner with the same gap. A brief becomes a synthetic curriculum topic
(`brief_topic`) and goes through the same content run as any topic: research, plan, steps, Critic.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic

BriefKind = Literal["remedial", "contrast"]
OrderStatus = Literal["ordered", "ready", "failed"]
MAX_TOPIC_ID = 90
REMEDIAL_STEPS = 1  # a mini-lesson on one gap
CONTRAST_STEPS = 2  # the two constructions, then telling them apart


class ContentBrief(BaseModel):
    kind: BriefKind
    topic_ids: list[str] = Field(min_length=1, max_length=2)
    error_type: str | None = Field(default=None, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    focus_ru: str = Field(min_length=5, max_length=160)  # what the lesson must make clear

    @model_validator(mode="after")
    def shape(self) -> ContentBrief:
        if self.kind == "remedial" and (len(self.topic_ids) != 1 or self.error_type is None):
            raise ValueError("a remedial names one topic and the error type it repairs")
        if self.kind == "contrast" and (len(self.topic_ids) != 2 or self.topic_ids[0] == self.topic_ids[1]):
            raise ValueError("a contrast names two different topics")
        return self

    @property
    def topic_id(self) -> str:
        """The synthetic topic id the content is stored under: the same for every learner with this gap."""
        topics = sorted(self.topic_ids) if self.kind == "contrast" else self.topic_ids
        parts = [self.kind, *topics]
        if self.error_type:
            parts.append(self.error_type)
        topic_id = "_".join(parts)
        if len(topic_id) > MAX_TOPIC_ID:
            topic_id = f"{topic_id[:77]}_{hashlib.sha256(topic_id.encode()).hexdigest()[:12]}"
        return topic_id

    @property
    def steps(self) -> int:
        return REMEDIAL_STEPS if self.kind == "remedial" else CONTRAST_STEPS


def brief_topic(brief: ContentBrief, topics: dict[str, CurriculumTopic]) -> CurriculumTopic:
    """The curriculum topic the content run writes for the brief (titles carry the focus)."""
    named = [topics[topic_id] for topic_id in brief.topic_ids]
    if brief.kind == "remedial":
        title_en, title_ru = f"{named[0].title_en}: {brief.error_type}", f"{named[0].title_ru}: {brief.focus_ru}"
    else:
        title_en = f"{named[0].title_en} vs {named[1].title_en}"
        title_ru = f"{named[0].title_ru} или {named[1].title_ru}: {brief.focus_ru}"
    tags = sorted({tag for topic in named for tag in topic.tags} | {brief.kind})
    return CurriculumTopic(id=brief.topic_id, title_en=title_en[:80], title_ru=title_ru[:80], order=1, tags=tags)


class ContentOrder(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_id: str
    cefr: Cefr
    brief: ContentBrief
    status: OrderStatus
    content_id: int | None
    created_at: datetime
    finished_at: datetime | None
