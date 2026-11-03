"""Database helpers for the storage tests. Every test database lives in a throwaway schema.

A schema of its own (not `public`) means a DATABASE_URL that points at a real database,
e.g. through the SSH tunnel, never has its tables migrated, rolled back or dropped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from english_agent.paths import repo_root


@contextmanager
def schema_engine(url: str) -> Iterator[Engine]:
    """An engine whose connections use a fresh schema; the schema is dropped afterwards."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def run_alembic(engine: Engine, action: Literal["upgrade", "downgrade"], revision: str) -> None:
    with engine.begin() as connection:
        config = Config(str(repo_root() / "alembic.ini"))
        config.attributes["connection"] = connection
        config.attributes["configure_logging"] = False
        if action == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)
