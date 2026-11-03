"""Engine and session plumbing. Sync SQLAlchemy over psycopg 3."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from english_agent.config import Settings

SessionFactory = sessionmaker[Session]


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url.get_secret_value()
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # pre_ping: the app lives for weeks, and Postgres restarts must not leave dead pooled connections.
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    # Records are built inside the session, but expire_on_commit=False keeps rows readable after it too.
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(sessions: SessionFactory) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    with sessions() as session, session.begin():
        yield session
