"""Engine and session plumbing. Async SQLAlchemy over psycopg 3 (the same postgresql+psycopg:// URL).

One engine per process, created at startup and disposed on shutdown. Its pool is bound to the
event loop that uses it first, so the whole process runs its database work on one loop.
Alembic keeps using a sync engine of its own (`migrations/env.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from english_agent.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url.get_secret_value()
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return create_async_engine(
        url,
        # pre_ping: the app lives for weeks, and Postgres restarts must not leave dead pooled connections.
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,  # below typical server/proxy idle timeouts
    )


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    # Records are built inside the session, but expire_on_commit=False keeps rows readable after it too.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(sessions: SessionFactory) -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, roll back on any exception (cancellation included)."""
    async with sessions() as session, session.begin():
        yield session
