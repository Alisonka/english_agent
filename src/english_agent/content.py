"""Shared lesson content: one lesson per topic, level and generation recipe, sent to every learner.

Research and writing take up to ~14 Claude calls per lesson; with shared content they run once
per (topic, level) instead of once per learner. Personal work (the quiz answers, the graded
translation, FSRS cards, the profile) stays per learner.

`content_key` names the recipe. When the lesson schema, a prompt or a model changes, the key
changes, and the next learner to reach the topic gets content written by the new recipe.
"""

from __future__ import annotations

import os
import socket

from english_agent.config import Settings
from english_agent.versions import SCHEMA_VERSIONS, prompt_fingerprint

LEGACY_CONTENT_KEY = "legacy"  # lessons backfilled from before shared content (migration 0005)


def content_key(settings: Settings) -> str:
    """The generation recipe: lesson schema, research and writer prompt fingerprints, both models."""
    return ":".join(
        (
            f"lesson{SCHEMA_VERSIONS['lesson']}",
            prompt_fingerprint("research_system"),
            prompt_fingerprint("write_lesson_system"),
            settings.research_model,
            settings.writer_model,
        )
    )


def worker_id() -> str:
    """Who holds a claim, for the logs and for spotting a dead worker's claims: host and process id.

    Not cached: a forked worker process must report its own pid.
    """
    return f"{socket.gethostname()}:{os.getpid()}"[:64]
