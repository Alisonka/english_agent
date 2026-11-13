"""The bot's side of the queue: hand slow work to the workers (see `bot.deps.JobDispatcher`)."""

from __future__ import annotations

from english_agent.jobs.dedup import Deduper, enqueue, grading_key, profile_key
from english_agent.jobs.tasks import GRADING_KEY_TTL, PROFILE_KEY_TTL, grade_translation, refresh_profile


class TaskiqJobs:
    def __init__(self, dedup: Deduper) -> None:
        self._dedup = dedup

    async def grade_translation(self, answer_id: int) -> None:
        await enqueue(self._dedup, grading_key(answer_id), GRADING_KEY_TTL, grade_translation, answer_id)

    async def refresh_profile(self, learner_id: int) -> None:
        await enqueue(self._dedup, profile_key(learner_id), PROFILE_KEY_TTL, refresh_profile, learner_id)
