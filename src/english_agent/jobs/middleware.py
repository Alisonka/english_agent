"""One middleware for every job: a trace span (continuing the enqueuer's trace), log fields,
metrics, and an alert when a job fails for good (no retry left, or not retryable)."""

from __future__ import annotations

import time
from contextvars import Token
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from english_agent.logs import pop, push
from english_agent.metrics import JOB_SECONDS, JOBS
from english_agent.tracing import get_tracer

_TRACE_LABEL = "otel"  # W3C trace context of the code that enqueued the job


class JobMiddleware(TaskiqMiddleware):
    def __init__(self, retryable: tuple[type[BaseException], ...], max_retries: int = 3) -> None:
        super().__init__()
        self._retryable, self._max_retries = retryable, max_retries
        self._running: dict[str, tuple[Span, Token[Context], Token[dict[str, Any] | None], float]] = {}

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        if carrier:
            message.labels[_TRACE_LABEL] = carrier
        return message

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        carrier = message.labels.get(_TRACE_LABEL)
        parent = propagate.extract(carrier) if isinstance(carrier, dict) else None
        span = get_tracer().start_span(f"job.{message.task_name}", context=parent, kind=SpanKind.CONSUMER)
        span.set_attribute("job.id", message.task_id)
        span.set_attribute("job.retries", int(message.labels.get("_retries", 0)))
        attached = otel_context.attach(trace.set_span_in_context(span))
        fields = push(job=message.task_name, job_id=message.task_id)
        self._running[message.task_id] = (span, attached, fields, time.monotonic())
        return message

    def _final(self, message: TaskiqMessage, exception: BaseException) -> bool:
        if not message.labels.get("retry_on_error") or not isinstance(exception, self._retryable):
            return True
        retries = int(message.labels.get("_retries", 0)) + 1
        return retries >= int(message.labels.get("max_retries", self._max_retries))

    async def on_error(self, message: TaskiqMessage, result: TaskiqResult[Any], exception: BaseException) -> None:
        running = self._running.get(message.task_id)
        if running is not None:
            running[0].record_exception(exception)
            running[0].set_status(Status(StatusCode.ERROR, str(exception)))
        if self._final(message, exception):
            deps = getattr(self.broker.state, "deps", None)
            alerts = getattr(getattr(deps, "runtime", None), "alerts", None)
            if alerts is not None:
                await alerts.alert(f"job {message.task_name} failed", f"{type(exception).__name__}: {exception}")

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        running = self._running.pop(message.task_id, None)
        JOBS.labels(message.task_name, "error" if result.is_err else "ok").inc()
        if running is None:
            return
        span, attached, fields, started = running
        JOB_SECONDS.labels(message.task_name).observe(time.monotonic() - started)
        span.end()
        otel_context.detach(attached)
        pop(fields)
