"""The learner's chosen settings, persisted as a small JSON file.

This is a deliberately minimal stand-in for the storage layer: a single learner,
one file. When the project grows a database (per-user state, review logs), this
file becomes one row. The daily pipeline reads the level from here; the bot's
`level` command writes it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from english_agent.models.cefr import Cefr


class LearnerState(BaseModel):
    cefr: Cefr = "A2"


def load_learner_state(path: Path) -> LearnerState | None:
    """The saved state, or None if the file is missing or unreadable.

    A None return lets callers fall back to a configured default instead of
    silently pretending the learner picked A2.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return LearnerState.model_validate_json(text)
    except ValidationError:
        return None


def save_learner_state(state: LearnerState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
