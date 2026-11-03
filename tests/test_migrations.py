from __future__ import annotations

from sqlalchemy import inspect

from db import run_alembic, schema_engine

TABLES = {"learners", "lessons", "items", "errors", "cards", "review_logs"}


def test_upgrade_downgrade_upgrade(database_url: str) -> None:
    with schema_engine(database_url) as engine:
        run_alembic(engine, "upgrade", "head")
        assert set(inspect(engine).get_table_names()) >= TABLES

        run_alembic(engine, "downgrade", "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

        run_alembic(engine, "upgrade", "head")
        assert set(inspect(engine).get_table_names()) >= TABLES
