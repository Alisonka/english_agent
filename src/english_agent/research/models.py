from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from english_agent.models.lesson import Source
from english_agent.versions import Provenance

NOT_FOUND_MESSAGE = "нет на сайте"

# контракты


class SearchHit(BaseModel):  # результат поискового шага
    url: str
    slug: str
    score: int


class PageContent(BaseModel):  # скачанное содержимое страницы
    url: str
    title: str
    text: str
    truncated: bool


class NoteExample(BaseModel):
    en: str = Field(min_length=1, max_length=240)
    ru: str | None = Field(default=None, max_length=240)


class TopicNotes(BaseModel):  # Это то, что research-подагент передаёт функции write_lesson.
    """What the research subagent hands to write_lesson. Grounded in exactly one fetched page."""

    site: Literal["englex", "skyeng", "skysmart"]
    source: Source
    page_title: str = Field(min_length=1, max_length=200)
    summary_ru: str = Field(min_length=40, max_length=2000)
    rule_points: list[str] = Field(min_length=3, max_length=15)
    # Exceptions, special cases, contrasts the page describes. Empty only if the page has none.
    nuances: list[str] = Field(default_factory=list, max_length=12)
    examples: list[NoteExample] = Field(min_length=2, max_length=20)
    typical_mistakes: list[str] = Field(default_factory=list, max_length=8)


class ToolCallRecord(BaseModel):  # запись об одном вызове инструмента
    step: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str


class ResearchResult(BaseModel):  # результат работы research-подагента
    topic_id: str
    status: Literal["found", "not_found", "failed"]
    message: str
    notes: TopicNotes | None = None
    sites_checked: list[str]
    steps: int
    trajectory: list[ToolCallRecord]
    # How this result was produced (prompt / model / schema versions).
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def consistent(self) -> ResearchResult:
        if self.status == "found" and self.notes is None:
            raise ValueError("status=found requires notes")
        if self.status != "found" and self.notes is not None:
            raise ValueError("notes are only allowed when status=found")
        if self.status == "not_found" and self.message != NOT_FOUND_MESSAGE:
            raise ValueError(f"status=not_found must carry message {NOT_FOUND_MESSAGE!r}")
        return self
