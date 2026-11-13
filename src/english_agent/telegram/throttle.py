"""Outgoing rate limits and flood-control handling for every Bot API request of a process.

Telegram allows about 30 messages a second per bot and about one a second per chat; beyond
that it answers 429 with `retry_after`. `ThrottlingMiddleware` waits for a slot before each
request that targets a chat (and for a global slot before any send), and when Telegram still
answers 429 it sleeps `retry_after` (capped) and retries a few times before giving up.
`getUpdates` is never throttled.

`RateLimiter` is a protocol: `LocalRateLimiter` keeps the budget in this process; a Redis one
lets the bot and the workers share it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 60
RETRIES_ON_FLOOD = 3
_sleep = asyncio.sleep  # module-level, so tests can wait out flood control instantly


class RateLimiter(Protocol):
    async def acquire(self, chat_id: str | None) -> None:
        """Return once one more request (to `chat_id`, if given) fits the budget."""


class _Bucket:
    """Token bucket: `rate` tokens a second, at most `burst` saved up."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate, self.burst, self._clock = rate, burst, clock
        self.tokens, self.updated = burst, clock()

    def wait_time(self) -> float:
        """Seconds until a token is free; takes it when the answer is 0."""
        now = self._clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class LocalRateLimiter:
    def __init__(
        self,
        *,
        global_per_second: float,
        chat_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_chats: int = 10_000,
    ) -> None:
        self._clock, self._sleep, self._max_chats = clock, sleep, max_chats
        self._global = _Bucket(global_per_second, max(1.0, global_per_second), clock)
        self._chat_rate = chat_per_second
        self._chats: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def _chat(self, chat_id: str) -> _Bucket:
        bucket = self._chats.get(chat_id)
        if bucket is None:
            if len(self._chats) >= self._max_chats:
                self._chats.clear()  # idle buckets are full anyway: forgetting them changes nothing
            bucket = self._chats[chat_id] = _Bucket(self._chat_rate, 1.0, self._clock)
        return bucket

    async def acquire(self, chat_id: str | None) -> None:
        while True:
            async with self._lock:
                wait = self._chat(chat_id).wait_time() if chat_id is not None else 0.0
                if wait == 0:
                    wait = self._global.wait_time()
                    if wait and chat_id is not None:
                        self._chat(chat_id).tokens += 1  # give the chat token back: nothing was sent
            if wait == 0:
                return
            await self._sleep(wait)


def _chat_of(method: TelegramMethod[TelegramType]) -> str | None:
    chat_id = getattr(method, "chat_id", None)
    return None if chat_id is None else str(chat_id)


class ThrottlingMiddleware(BaseRequestMiddleware):
    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if isinstance(method, GetUpdates):
            return await make_request(bot, method)
        attempt = 0
        while True:
            await self._limiter.acquire(_chat_of(method))
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as error:
                attempt += 1
                if attempt > RETRIES_ON_FLOOD:
                    raise
                delay = min(error.retry_after, MAX_RETRY_AFTER_SECONDS)
                log.warning("telegram flood control on %s, retrying in %ss", type(method).__name__, delay)
                await _sleep(delay)
