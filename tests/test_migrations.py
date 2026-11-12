from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import inspect, text

from db import run_alembic, schema_engine

TABLES = {
    "learners",
    "lessons",
    "items",
    "errors",
    "cards",
    "review_logs",
    "learner_profiles",
    "practice_deliveries",
    "quiz_polls",
    "quiz_answers",
    "translation_answers",
}


def test_upgrade_downgrade_upgrade(database_url: str) -> None:
    with schema_engine(database_url) as engine:
        run_alembic(engine, "upgrade", "head")
        assert set(inspect(engine).get_table_names()) >= TABLES

        run_alembic(engine, "downgrade", "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}

        run_alembic(engine, "upgrade", "head")
        assert set(inspect(engine).get_table_names()) >= TABLES


def test_existing_learners_count_as_started_after_0004(database_url: str) -> None:
    with schema_engine(database_url) as engine:
        run_alembic(engine, "upgrade", "0003")
        with engine.begin() as connection:
            with_lesson, without = (
                connection.execute(
                    text(
                        "INSERT INTO learners (telegram_chat_id, cefr, created_at)"
                        " VALUES (:chat, 'A2', :at) RETURNING id"
                    ),
                    {"chat": chat, "at": datetime(2026, 11, 2, 21, 30, tzinfo=UTC)},  # 00:30 on the 3rd in Moscow
                ).scalar_one()
                for chat in ("1", "2")
            )
            connection.execute(
                text(
                    "INSERT INTO lessons (learner_id, lesson_date, topic_id, cefr, schema_version, status)"
                    " VALUES (:learner, '2026-10-27', 'there_is_are', 'A2', 3, 'sent')"
                ),
                {"learner": with_lesson},
            )

        run_alembic(engine, "upgrade", "0004")
        with engine.connect() as connection:
            rows = dict(connection.execute(text("SELECT id, curriculum_started_on FROM learners")).tuples())
        assert rows == {with_lesson: date(2026, 10, 27), without: date(2026, 11, 3)}
