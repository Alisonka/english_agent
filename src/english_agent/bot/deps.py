"""What the bot's handlers depend on, and where they hand off slow work.

Handlers must answer within milliseconds: grading a translation takes a model call, so a
handler only stores the answer and calls `JobDispatcher.grade_translation(answer_id)`.
`InProcessJobs` runs that as a background task of the bot process, at most a few at a time,
and waits for them on shutdown; a queue-backed dispatcher can replace it without touching
the handlers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from english_agent.arbiter import arbitrate
from english_agent.config import Settings
from english_agent.examiner import continue_exam
from english_agent.grading import Grader, grade_answer, refresh_profile_safely
from english_agent.interlocutor import Tutor
from english_agent.llm import ChatLLM
from english_agent.reflector import Reflector
from english_agent.storage.repositories import Stores
from english_agent.telegram.sender import Sender

log = logging.getLogger(__name__)


class JobDispatcher(Protocol):
    async def grade_translation(self, answer_id: int) -> None: ...

    async def refresh_profile(self, learner_id: int) -> None: ...

    async def arbitrate(self, answer_id: int) -> None: ...

    async def answer_question(self, learner_id: int, text: str) -> None: ...

    async def continue_exam(self, exam_id: int) -> None: ...


@dataclass(frozen=True)
class BotDeps:
    settings: Settings
    stores: Stores
    sender: Sender
    jobs: JobDispatcher
    available_levels: list[str]


class InProcessJobs:
    """Background tasks of this process, bounded, logged, and awaited on shutdown."""

    def __init__(
        self,
        stores: Stores,
        sender: Sender,
        grade: Grader,
        *,
        reflect: Reflector | None = None,
        arbiter: Grader | None = None,
        tutor: Tutor | None = None,
        exam_writer: ChatLLM | None = None,
        concurrency: int = 4,
    ) -> None:
        self._stores, self._sender, self._grade, self._reflect = stores, sender, grade, reflect
        self._arbiter, self._tutor, self._exam_writer = arbiter, tutor, exam_writer
        self._gate = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, name: str, work: Coroutine[Any, Any, object]) -> None:
        async def run() -> None:
            started = False
            try:
                async with self._gate:
                    started = True
                    await work
            except Exception:
                log.exception("background job %s failed", name)
            finally:
                if not started:
                    work.close()  # cancelled while queued: the coroutine never ran

        task = asyncio.create_task(run(), name=name)
        self._tasks.add(task)  # a strong reference, or the task may be garbage-collected mid-run
        task.add_done_callback(self._tasks.discard)

    async def grade_translation(self, answer_id: int) -> None:
        self._spawn(
            f"grade:{answer_id}",
            grade_answer(self._stores, self._sender, self._grade, answer_id, reflect=self._reflect),
        )

    async def refresh_profile(self, learner_id: int) -> None:
        self._spawn(f"profile:{learner_id}", refresh_profile_safely(self._stores, learner_id, self._reflect))

    async def arbitrate(self, answer_id: int) -> None:
        if self._arbiter is None:
            log.info("dispute on answer %s: the Arbiter is off", answer_id)
            return
        self._spawn(f"arbiter:{answer_id}", arbitrate(self._stores, self._sender, self._arbiter, answer_id))

    async def answer_question(self, learner_id: int, text: str) -> None:
        if self._tutor is None:
            return
        self._spawn(f"tutor:{learner_id}", self._tutor(learner_id, text))

    async def continue_exam(self, exam_id: int) -> None:
        self._spawn(f"exam:{exam_id}", continue_exam(self._stores, self._sender, exam_id, self._exam_writer))

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for running jobs, then cancel the rest (a pending answer is regraded later)."""
        if not self._tasks:
            return
        _, still_running = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            log.warning("cancelled %d background job(s) on shutdown", len(still_running))
            await asyncio.gather(*still_running, return_exceptions=True)
