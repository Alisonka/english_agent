"""The TaskIQ broker and scheduler. Imported by the worker, the scheduler and the bot.

Redis Streams with a consumer group: a message is acknowledged after its job ran, and one a
dead worker never acknowledged is handed to another after JOB_IDLE_TIMEOUT. Without REDIS_URL
(tests, a quick local run) the broker is in-memory and runs jobs in the calling process.
"""

from __future__ import annotations

from taskiq import AsyncBroker, InMemoryBroker, SmartRetryMiddleware, TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from english_agent.config import Settings, get_settings
from english_agent.jobs.deps import open_worker_deps
from english_agent.jobs.middleware import JobMiddleware
from english_agent.llm import LLMUnavailable
from english_agent.logs import configure_logging
from english_agent.metrics import serve_metrics
from english_agent.tracing import configure_tracing

QUEUE = "english_agent"
RETRYABLE = (LLMUnavailable, ConnectionError, TimeoutError)
# A lesson can take ~10 minutes of research and writing; a message is re-delivered only after this.
JOB_IDLE_TIMEOUT_MS = 30 * 60 * 1000


def make_broker(settings: Settings) -> AsyncBroker:
    url = settings.redis_url.get_secret_value()
    if not url:
        return InMemoryBroker().with_middlewares(JobMiddleware(RETRYABLE))
    broker = RedisStreamBroker(
        url=url,
        queue_name=QUEUE,
        consumer_group_name="workers",
        idle_timeout=JOB_IDLE_TIMEOUT_MS,
        maxlen=100_000,  # trimmed: acknowledged history is never read again
    )
    # Transient failures only, and only for jobs labelled retry_on_error; the scheduler's next tick
    # is the main retry loop, since every piece of work's state is in PostgreSQL.
    retries = SmartRetryMiddleware(
        default_retry_count=3,
        default_delay=30,
        use_jitter=True,
        use_delay_exponent=True,
        max_delay_exponent=300,
        types_of_exceptions=RETRYABLE,
    )
    return broker.with_middlewares(retries, JobMiddleware(RETRYABLE))


broker = make_broker(get_settings())
scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker)])


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    settings = get_settings()
    if not settings.redis_url.get_secret_value():
        return  # the in-memory broker: whoever runs jobs in-process sets `state.deps` itself (tests)
    configure_logging(settings)
    state.tracing = configure_tracing(settings)
    serve_metrics(settings.metrics_port)
    state.deps = await open_worker_deps(settings)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    deps = getattr(state, "deps", None)
    if deps is not None:
        await deps.close()
    if getattr(state, "tracing", None) is not None:
        state.tracing.shutdown()
