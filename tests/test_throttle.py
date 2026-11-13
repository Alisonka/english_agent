from __future__ import annotations

from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod

from english_agent.telegram import throttle
from english_agent.telegram.sender import Sender
from english_agent.telegram.throttle import LocalRateLimiter
from telegram_fake import FakeTelegram, fake_bot


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


async def test_one_message_a_second_per_chat() -> None:
    clock = Clock()
    limiter = LocalRateLimiter(global_per_second=30, chat_per_second=1, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await limiter.acquire("42")
    assert clock.slept == [1.0, 1.0]


async def test_different_chats_only_share_the_global_budget() -> None:
    clock = Clock()
    limiter = LocalRateLimiter(global_per_second=2, chat_per_second=1, clock=clock, sleep=clock.sleep)
    for chat in ("1", "2", "3"):
        await limiter.acquire(chat)
    assert clock.slept == [0.5]  # the third chat waited for a global slot, not a per-chat one


async def test_requests_without_a_chat_are_not_held_by_chat_limits() -> None:
    clock = Clock()
    limiter = LocalRateLimiter(global_per_second=30, chat_per_second=1, clock=clock, sleep=clock.sleep)
    await limiter.acquire("42")
    await limiter.acquire(None)
    assert clock.slept == []


def flood_once(retry_after: int) -> Any:
    answered: list[bool] = []

    def rule(method: TelegramMethod[Any]) -> tuple[int, dict[str, Any]] | None:
        if answered:
            return None
        answered.append(True)
        return 429, {"error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": retry_after}}

    return rule


async def test_flood_control_is_waited_out_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(throttle, "_sleep", sleep)
    telegram = FakeTelegram(fail=flood_once(retry_after=3))

    assert await Sender(fake_bot(telegram)).send_text("42", "hi") == 1
    assert slept == [3] and len(telegram.of(SendMessage)) == 2


async def test_endless_flood_control_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(throttle, "_sleep", sleep)
    flood = {"error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 999}}
    always = FakeTelegram(fail=lambda method: (429, flood))

    with pytest.raises(TelegramRetryAfter):
        await Sender(fake_bot(always)).send_text("42", "hi")
    assert len(always.calls) == throttle.RETRIES_ON_FLOOD + 1
