"""Central registry for versioning the pipeline's components.

Rather than scattering version numbers across the codebase, this module is the one
place that knows:

- the schema version of each data model (bump when a model's shape changes),
- the content fingerprint of each prompt (derived automatically from the file, so
  it can never go stale),
- how to assemble a `Provenance` "passport" that is attached to pipeline outputs
  (`Lesson`, `ResearchResult`) for reproducibility and traceability.

The package version (SemVer) lives in `pyproject.toml` / `__init__.__version__`
and answers a different question: "which release of the code is this?". Provenance
answers "which prompt / model / schema / curriculum produced this artifact?".
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from english_agent import __version__

# Schema versions. Bump the relevant entry whenever the corresponding model's
# shape changes in a way that downstream consumers must notice.
SCHEMA_VERSIONS: dict[str, int] = {
    "lesson": 3,  # v3: added quiz and translation (practice sent after the lesson)
    "research_result": 1,
    "topic_notes": 1,
    # Storage records (models/storage.py); the table shapes themselves are versioned by Alembic.
    "learner_record": 1,
    "lesson_record": 1,
    "item_record": 1,
    "error_record": 1,
    "card_record": 1,
    "review_log_record": 1,
    "practice_delivery_record": 1,
    "quiz_poll_record": 1,
    "translation_answer_record": 1,
    "translation_grade": 1,
}

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Named prompts whose content we fingerprint. Add new prompts here.
PROMPT_FILES: dict[str, Path] = {
    "research_system": _PROMPTS_DIR / "research_system.md",
    "write_lesson_system": _PROMPTS_DIR / "write_lesson_system.md",
    "grade_translation_system": _PROMPTS_DIR / "grade_translation_system.md",
}


def fingerprint_text(text: str) -> str:
    """Short, stable content fingerprint (first 12 hex chars of sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def prompt_fingerprint(name: str) -> str:
    """Fingerprint of a named prompt, read from disk so it is always current."""
    return fingerprint_text(PROMPT_FILES[name].read_text(encoding="utf-8"))


class Provenance(BaseModel):
    """How a pipeline artifact was produced.

    Attached to `Lesson` and `ResearchResult` so any stored output can be traced
    back to the exact prompt / model / schema / curriculum that produced it.
    """

    pipeline_version: str = __version__
    component: str  # "research" | "writer"
    schema_version: int
    model: str | None = None  # resolved LLM model id, when known
    prompts: dict[str, str] = Field(default_factory=dict)  # prompt name -> fingerprint
    curriculum_version: int | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def build_provenance(
    *,
    component: str,
    schema_version: int,
    model: str | None = None,
    prompt_names: tuple[str, ...] = (),
    curriculum_version: int | None = None,
) -> Provenance:
    """Assemble a `Provenance` passport for one component of the pipeline."""
    return Provenance(
        component=component,
        schema_version=schema_version,
        model=model,
        prompts={name: prompt_fingerprint(name) for name in prompt_names},
        curriculum_version=curriculum_version,
    )
