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
    "lesson": 3,  # v3: added quiz and translation; frozen — legacy payloads only, superseded by topic steps
    "research_result": 1,
    "topic_notes": 1,
    # A topic taught in steps (models/topic.py): plan, per-step message + bank, and the cheat sheet.
    "topic_plan": 1,
    "lesson_step": 2,  # v2: review (the Critic's code-owned review)
    "topic_summary": 2,  # v2: review
    "topic_content": 2,  # v2: steps and summary carry the Critic's review
    # The Critic (models/review.py) and the content run report (models/run.py).
    "step_review": 1,
    "summary_review": 1,
    "content_run": 1,
    # Storage records (models/storage.py); the table shapes themselves are versioned by Alembic.
    "learner_record": 2,  # v2: schedule (timezone, send times, quiz delay), started_on, is_active, paused_at
    "learner_schedule": 1,
    "lesson_record": 4,  # v2: content_id; v3: status/claim/attempts; v4: step columns + payload_kind
    "lesson_content_record": 3,  # v2: payload_kind (lesson | topic); v3: run (the generation report)
    "item_record": 1,
    "error_record": 1,
    "evidence_record": 1,  # models/storage.py: one learner event that agent decisions cite
    "card_record": 2,  # v2: last_reviewed_at, reviews
    "review_log_record": 3,  # v2: source_ref; v3: payload.before (the card before the rating)
    "practice_delivery_record": 2,  # v2: payload (the day's assembled practice set)
    # The day's assembled quiz/translation set in that payload; v2: reason per item, provenance;
    # v3: profile_version and the "focus" reason.
    "practice_block": 3,
    "quiz_poll_record": 2,  # v2: item_id
    "translation_answer_record": 2,  # v2: grading_attempts, grading_started_at
    "translation_grade": 1,
    "grading_input": 2,  # what the grader sees: the day's sentences, their context; v2: learner (Reflection)
    "learner_profile": 2,  # v2: reflection (the Reflector's focus points and notes) replaces notes_ru
    "reflection": 1,
}

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Named prompts whose content we fingerprint. Add new prompts here.
PROMPT_FILES: dict[str, Path] = {
    "research_system": _PROMPTS_DIR / "research_system.md",
    "grade_translation_system": _PROMPTS_DIR / "grade_translation_system.md",
    "plan_topic_system": _PROMPTS_DIR / "plan_topic_system.md",
    "write_step_system": _PROMPTS_DIR / "write_step_system.md",
    "write_summary_system": _PROMPTS_DIR / "write_summary_system.md",
    "review_step_system": _PROMPTS_DIR / "review_step_system.md",
    "review_summary_system": _PROMPTS_DIR / "review_summary_system.md",
    "reflect_system": _PROMPTS_DIR / "reflect_system.md",
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
