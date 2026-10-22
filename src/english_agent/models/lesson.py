from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
EXAMPLE_MIN = 4
EXAMPLE_MAX = 8


class Source(BaseModel):
    url: HttpUrl
    title: str = Field(min_length=1, max_length=200)
    accessed_at: date

    @field_validator("accessed_at", mode="before")
    @classmethod
    def coerce_accessed_at(cls, value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return value


class Example(BaseModel):
    en: str = Field(min_length=1, max_length=240)
    ru: str = Field(min_length=1, max_length=240)
    highlights: list[str] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def highlights_appear_in_sentence(self) -> "Example":
        missing = [part for part in self.highlights if part not in self.en]
        if missing:
            raise ValueError(f"highlights must be substrings of en: {missing}")
        return self


class Lesson(BaseModel):
    """Structured daily grammar lesson. The Telegram message is rendered from these fields."""

    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title_en: str = Field(min_length=1, max_length=80)
    title_ru: str = Field(min_length=1, max_length=80)
    cefr: Literal["A2"]
    rule_ru: str = Field(min_length=300, max_length=2000)  # full theory: forms, uses, contractions
    form: str = Field(min_length=1, max_length=160)
    # Tricky cases, exceptions, contrasts with similar constructions and with Russian.
    nuances_ru: Annotated[list[Annotated[str, Field(min_length=20, max_length=400)]], Field(min_length=3, max_length=8)]
    examples: Annotated[list[Example], Field(min_length=EXAMPLE_MIN, max_length=EXAMPLE_MAX)]
    mistake_ru: str = Field(min_length=10, max_length=400)
    incorrect: str = Field(min_length=1, max_length=240)
    correct: str = Field(min_length=1, max_length=240)
    sources: Annotated[list[Source], Field(min_length=1, max_length=5)]
