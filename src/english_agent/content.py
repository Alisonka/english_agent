"""Shared lesson content: one lesson per topic, level and generation recipe, sent to every learner.

Research, writing and the Critic's reviews take a few dozen Claude calls per topic; with shared content they run once
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
    """The generation recipe: topic-content schema, every prompt of the run, the models (the Critic's too)."""
    return ":".join(
        (
            f"topic{SCHEMA_VERSIONS['topic_content']}",
            prompt_fingerprint("research_system"),
            prompt_fingerprint("plan_topic_system"),
            prompt_fingerprint("write_step_system"),
            prompt_fingerprint("write_summary_system"),
            prompt_fingerprint("review_step_system"),
            prompt_fingerprint("review_summary_system"),
            settings.research_model,
            settings.writer_model,
            settings.critic_model or "no-critic",
        )
    )


def worker_id() -> str:
    """Who holds a claim, for the logs and for spotting a dead worker's claims: host and process id.

    Not cached: a forked worker process must report its own pid.
    """
    return f"{socket.gethostname()}:{os.getpid()}"[:64]
