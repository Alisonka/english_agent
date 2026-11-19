"""The bot's side of the queue: hand slow work to the workers (see `bot.deps.JobDispatcher`)."""

from __future__ import annotations

from english_agent.jobs.dedup import Deduper, arbiter_key, enqueue, grading_key, profile_key
from english_agent.jobs.tasks import (
    GRADING_KEY_TTL,
    PROFILE_KEY_TTL,
    answer_tutor_question,
    arbitrate_dispute,
    grade_translation,
    refresh_profile,
)


class TaskiqJobs:
    def __init__(self, dedup: Deduper) -> None:
        self._dedup = dedup

    async def answer_question(self, learner_id: int, text: str) -> None:
        await answer_tutor_question.kiq(learner_id, text)  # every question counts: no de-duplication

    async def arbitrate(self, answer_id: int) -> None:
        await enqueue(self._dedup, arbiter_key(answer_id), GRADING_KEY_TTL, arbitrate_dispute, answer_id)

    async def grade_translation(self, answer_id: int) -> None:
        await enqueue(self._dedup, grading_key(answer_id), GRADING_KEY_TTL, grade_translation, answer_id)

    async def refresh_profile(self, learner_id: int) -> None:
        await enqueue(self._dedup, profile_key(learner_id), PROFILE_KEY_TTL, refresh_profile, learner_id)
