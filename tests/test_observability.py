"""Metrics, alerts and the job middleware."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.methods import SendMessage
from taskiq import TaskiqMessage, TaskiqResult

from english_agent.alerts import Alerter
from english_agent.jobs.middleware import JobMiddleware
from english_agent.llm import LLMRejected, LLMUnavailable
from english_agent.metrics import JOBS, TELEGRAM_REQUESTS
from english_agent.telegram.sender import Sender, TelegramForbiddenError
from telegram_fake import FakeTelegram, blocked_by, fake_bot


def value(counter: Any, *labels: str) -> float:
    return float(counter.labels(*labels)._value.get())


async def test_telegram_requests_are_counted_by_outcome() -> None:
    before_ok = value(TELEGRAM_REQUESTS, "sendMessage", "ok")
    before_blocked = value(TELEGRAM_REQUESTS, "sendMessage", "TelegramForbiddenError")
    sender = Sender(fake_bot(FakeTelegram(fail=blocked_by("7"))))
    await sender.send_text("42", "hi")
    with pytest.raises(TelegramForbiddenError):
        await sender.send_text("7", "hi")
    assert value(TELEGRAM_REQUESTS, "sendMessage", "ok") == before_ok + 1
    assert value(TELEGRAM_REQUESTS, "sendMessage", "TelegramForbiddenError") == before_blocked + 1


async def test_an_alert_is_sent_once_per_quiet_period_and_always_logged() -> None:
    telegram = FakeTelegram()
    alerts = Alerter(Sender(fake_bot(telegram)), "1", quiet_seconds=600)
    await alerts.alert("job failed", "boom <1>")
    await alerts.alert("job failed", "boom again")
    await alerts.alert("lesson not written", "topic")
    texts = [call.text for call in telegram.of(SendMessage)]
    assert len(texts) == 2 and "boom &lt;1&gt;" in texts[0]


async def test_no_admin_chat_means_the_alert_is_only_logged() -> None:
    telegram = FakeTelegram()
    await Alerter(Sender(fake_bot(telegram)), "").alert("job failed", "boom")
    assert telegram.calls == []


class Alerts:
    def __init__(self) -> None:
        self.raised: list[str] = []

    async def alert(self, kind: str, text: str) -> None:
        self.raised.append(kind)


def middleware(alerts: Alerts) -> JobMiddleware:
    job = JobMiddleware((LLMUnavailable,))
    job.set_broker(SimpleNamespace(state=SimpleNamespace(deps=SimpleNamespace(runtime=SimpleNamespace(alerts=alerts)))))  # type: ignore[arg-type]
    return job


def message(**labels: Any) -> TaskiqMessage:
    return TaskiqMessage(task_id="t1", task_name="grade_translation", labels=labels, args=[1], kwargs={})


async def test_a_job_that_will_be_retried_raises_no_alert_but_its_last_failure_does() -> None:
    alerts = Alerts()
    job = middleware(alerts)
    result: TaskiqResult[Any] = TaskiqResult(is_err=True, return_value=None, execution_time=0.1)
    before = value(JOBS, "grade_translation", "error")

    first = job.pre_execute(message(retry_on_error=True, _retries=0))
    await job.on_error(first, result, LLMUnavailable("529"))
    job.post_execute(first, result)
    assert alerts.raised == []

    last = job.pre_execute(message(retry_on_error=True, _retries=2))
    await job.on_error(last, result, LLMUnavailable("529"))
    job.post_execute(last, result)
    assert alerts.raised == ["job grade_translation failed"]
    assert value(JOBS, "grade_translation", "error") == before + 2


async def test_a_job_failing_for_a_reason_retrying_cannot_fix_alerts_at_once() -> None:
    alerts = Alerts()
    job = middleware(alerts)
    result: TaskiqResult[Any] = TaskiqResult(is_err=True, return_value=None, execution_time=0.1)
    failed = job.pre_execute(message(retry_on_error=True, _retries=0))
    await job.on_error(failed, result, LLMRejected("400"))
    job.post_execute(failed, result)
    assert alerts.raised == ["job grade_translation failed"]
