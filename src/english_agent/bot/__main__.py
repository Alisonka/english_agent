"""The bot process: answers updates and hands slow work to the job queue.

    uv run python -m english_agent.bot

Long polling by default (this process must then be the bot's only `getUpdates` consumer);
with TELEGRAM_WEBHOOK_URL set it registers that webhook and serves it on TELEGRAM_WEBHOOK_PORT
instead, which also lets several bot processes share the load behind a proxy. With REDIS_URL,
gradings go to the workers and the rate budget is shared with them; without it (a local run)
gradings run in this process. SIGTERM finishes the updates in progress and closes everything.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from redis.asyncio import Redis

from english_agent.bot.app import ALLOWED_UPDATES, build_dispatcher
from english_agent.bot.deps import BotDeps, InProcessJobs, JobDispatcher
from english_agent.config import Settings, get_settings
from english_agent.grading import make_grader
from english_agent.health import beat
from english_agent.llm import make_client
from english_agent.logs import configure_logging
from english_agent.paths import available_cefr_levels
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import Stores, sql_stores
from english_agent.telegram.sender import Sender, make_bot
from english_agent.telegram.throttle import RateLimiter, RedisRateLimiter
from english_agent.tracing import configure_tracing

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 20  # below compose's stop_grace_period for the bot
MAX_CONCURRENT_UPDATES = 100  # updates handled at once; more wait in aiogram's queue
WEBHOOK_PATH = "/telegram"


async def _jobs(
    settings: Settings, stack: AsyncExitStack, stores: Stores, sender: Sender, redis: Redis | None
) -> JobDispatcher:
    if redis is None:
        client = make_client(settings)
        stack.push_async_callback(client.close)
        jobs = InProcessJobs(stores, sender, make_grader(settings, client))
        stack.push_async_callback(jobs.drain, SHUTDOWN_GRACE_SECONDS)
        return jobs
    from english_agent.jobs.broker import broker  # imported only with a queue to talk to
    from english_agent.jobs.dedup import RedisDeduper
    from english_agent.jobs.dispatch import TaskiqJobs

    await broker.startup()
    stack.push_async_callback(broker.shutdown)
    return TaskiqJobs(RedisDeduper(redis))


async def _healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _serve_webhook(settings: Settings, dispatcher: Dispatcher, bot: Bot) -> None:
    secret = settings.telegram_webhook_secret.get_secret_value() or None
    app = web.Application()
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(app, path=WEBHOOK_PATH)
    app.router.add_get("/healthz", _healthz)
    setup_application(app, dispatcher, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.telegram_webhook_port).start()
    await bot.set_webhook(
        settings.telegram_webhook_url, secret_token=secret, allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False
    )
    log.info("serving the webhook on :%d%s", settings.telegram_webhook_port, WEBHOOK_PATH)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()  # finishes requests in flight


async def serve(settings: Settings) -> int:
    async with AsyncExitStack() as stack:
        engine = make_engine(settings)
        stack.push_async_callback(engine.dispose)
        redis_url = settings.redis_url.get_secret_value()
        redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        limiter: RateLimiter | None = None
        if redis is not None:
            stack.push_async_callback(redis.aclose)
            limiter = RedisRateLimiter(
                redis,
                global_per_second=settings.telegram_global_rate_per_second,
                chat_per_second=settings.telegram_chat_rate_per_second,
            )
        bot = make_bot(settings, limiter=limiter)
        stack.push_async_callback(bot.session.close)
        stores = sql_stores(make_session_factory(engine), make_scheduler(settings))
        sender = Sender(bot)
        jobs = await _jobs(settings, stack, stores, sender, redis)
        dispatcher = build_dispatcher(BotDeps(settings, stores, sender, jobs, available_cefr_levels()))
        heartbeat = asyncio.create_task(beat(), name="heartbeat")
        stack.callback(heartbeat.cancel)

        if settings.telegram_webhook_url:
            await _serve_webhook(settings, dispatcher, bot)
        else:
            await bot.delete_webhook(drop_pending_updates=False)  # polling and a webhook cannot coexist
            log.info("polling for updates")
            await dispatcher.start_polling(
                bot,
                allowed_updates=ALLOWED_UPDATES,
                handle_signals=True,
                close_bot_session=False,
                tasks_concurrency_limit=MAX_CONCURRENT_UPDATES,
            )
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    if not settings.telegram_bot_token.get_secret_value():
        log.error("TELEGRAM_BOT_TOKEN must be set")
        return 2
    if not settings.database_url.get_secret_value():
        log.error("DATABASE_URL must be set")
        return 2
    provider = configure_tracing(settings)
    try:
        return asyncio.run(serve(settings))
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
