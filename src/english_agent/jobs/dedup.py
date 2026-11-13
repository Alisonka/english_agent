"""De-duplication keys: one queued-or-running job per piece of work.

The scheduler runs every minute; a lesson job can take minutes. `enqueue` sets the work's key
(SET NX with a TTL) before sending the job, and the job deletes it when it ends, so the next tick
neither piles up copies of a running job nor waits a full TTL to retry a finished one. The TTL
only bounds how long a key outlives a worker that died mid-job.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Protocol

from redis.asyncio import Redis
from taskiq import AsyncTaskiqDecoratedTask


class Deduper(Protocol):
    async def claim(self, key: str, ttl_seconds: int) -> bool: ...

    async def release(self, key: str) -> None: ...


class RedisDeduper:
    def __init__(self, redis: Redis, prefix: str = "job") -> None:
        self._redis, self._prefix = redis, prefix

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self._redis.set(f"{self._prefix}:{key}", "1", nx=True, ex=ttl_seconds))

    async def release(self, key: str) -> None:
        await self._redis.delete(f"{self._prefix}:{key}")


class LocalDeduper:
    """The same rules in memory, for one process (tests, a bot without Redis)."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        if self._until.get(key, 0) > now:
            return False
        self._until[key] = now + ttl_seconds
        return True

    async def release(self, key: str) -> None:
        self._until.pop(key, None)


async def enqueue(
    dedup: Deduper, key: str, ttl_seconds: int, task: AsyncTaskiqDecoratedTask[Any, Any], *args: Any
) -> bool:
    """Send the job unless the same work is queued or running; True when it was sent."""
    if not await dedup.claim(key, ttl_seconds):
        return False
    try:
        await task.kiq(*args)
    except BaseException:
        await dedup.release(key)
        raise
    return True


def lesson_key(learner_id: int, day: date) -> str:
    return f"lesson:{learner_id}:{day.isoformat()}"


def practice_key(learner_id: int, day: date) -> str:
    return f"practice:{learner_id}:{day.isoformat()}"


def grading_key(answer_id: int) -> str:
    return f"grade:{answer_id}"


def profile_key(learner_id: int) -> str:
    return f"profile:{learner_id}"


def content_key_for(topic_id: str, cefr: str) -> str:
    return f"content:{topic_id}:{cefr}"
