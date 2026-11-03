from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from english_agent.models.cefr import Cefr
from english_agent.paths import curriculum_path


class CurriculumTopic(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title_en: str = Field(min_length=1, max_length=80)
    title_ru: str = Field(min_length=1, max_length=80)
    order: int = Field(ge=1)
    tags: list[str] = Field(min_length=1)


class Curriculum(BaseModel):
    version: int = Field(ge=1)
    cefr: Cefr
    explanation_language: str
    example_language: str
    topics: list[CurriculumTopic] = Field(min_length=40, max_length=80)

    @model_validator(mode="after")
    def unique_ids_and_orders(self) -> "Curriculum":
        ids = [topic.id for topic in self.topics]
        orders = [topic.order for topic in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("topic ids must be unique")
        if len(orders) != len(set(orders)):
            raise ValueError("topic orders must be unique")
        expected = list(range(1, len(self.topics) + 1))
        if sorted(orders) != expected:
            raise ValueError("topic orders must be a contiguous sequence starting at 1")
        return self

    def topic_ids(self) -> list[str]:
        return [topic.id for topic in sorted(self.topics, key=lambda item: item.order)]


def load_curriculum(cefr: Cefr = "A2", *, path: Path | None = None) -> Curriculum:
    target = path or curriculum_path(cefr)
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    curriculum = Curriculum.model_validate(payload)
    if path is None and curriculum.cefr != cefr:
        raise ValueError(f"{target.name} declares cefr {curriculum.cefr!r}, expected {cefr!r}")
    return curriculum
