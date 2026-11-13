"""What a worker process holds for its jobs: built once at worker startup, closed at shutdown."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends

from english_agent.alerts import Alerter
from english_agent.config import Settings
from english_agent.grading import Grader, make_grader
from english_agent.health import beat
from english_agent.jobs.dedup import Deduper, RedisDeduper
from english_agent.llm import make_client
from english_agent.pipeline import Runtime
from english_agent.research.sitemaps import RedisSitemapCache
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import sql_stores
from english_agent.telegram.sender import Sender, make_bot
from english_agent.telegram.throttle import RedisRateLimiter
from english_agent.tracing import instrument_engine


@dataclass
class WorkerDeps:
    runtime: Runtime
    dedup: Deduper
    grade: Grader
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def close(self) -> None:
        await self._stack.aclose()


async def open_worker_deps(settings: Settings) -> WorkerDeps:
    """The database pool, Redis, the bot (with the shared rate budget) and the Claude client."""
    stack = AsyncExitStack()
    try:
        redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
        stack.push_async_callback(redis.aclose)
        engine = make_engine(settings)
        stack.push_async_callback(engine.dispose)
        limiter = RedisRateLimiter(
            redis,
            global_per_second=settings.telegram_global_rate_per_second,
            chat_per_second=settings.telegram_chat_rate_per_second,
        )
        bot = make_bot(settings, limiter=limiter)
        stack.push_async_callback(bot.session.close)
        client = make_client(settings)
        stack.push_async_callback(client.close)
        heartbeat = asyncio.create_task(beat(), name="heartbeat")
        stack.callback(heartbeat.cancel)
        instrument_engine(engine)
        sender = Sender(bot)
        runtime = Runtime(
            settings,
            sql_stores(make_session_factory(engine), make_scheduler(settings)),
            sender,
            client=client,
            sitemaps=RedisSitemapCache(redis, ttl_days=settings.sitemap_ttl_days),
            alerts=Alerter(sender, settings.admin_chat_id),
        )
        return WorkerDeps(runtime, RedisDeduper(redis), make_grader(settings, client), stack)
    except BaseException:
        await stack.aclose()
        raise


def worker_deps(context: Context = TaskiqDepends()) -> WorkerDeps:
    """Inject the worker's deps into a task (`deps: WorkerDeps = TaskiqDepends(worker_deps)`)."""
    deps = getattr(context.state, "deps", None)
    if not isinstance(deps, WorkerDeps):
        raise RuntimeError("worker deps are not set up: is this a worker process with REDIS_URL set?")
    return deps
