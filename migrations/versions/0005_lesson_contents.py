"""shared lesson content: lesson_contents, written once per topic, level and recipe; lessons.content_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lesson_contents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("cefr", sa.String(length=2), nullable=False),
        sa.Column("curriculum_version", sa.Integer(), nullable=False),
        sa.Column("content_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('generating', 'ready', 'failed')", name="ck_lesson_contents_status"),
        sa.PrimaryKeyConstraint("id", name="pk_lesson_contents"),
        sa.UniqueConstraint(
            "topic_id",
            "cefr",
            "curriculum_version",
            "content_key",
            name="uq_lesson_contents_topic_id_cefr_curriculum_version_content_key",
        ),
    )
    op.add_column("lessons", sa.Column("content_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_lessons_content_id_lesson_contents",
        "lessons",
        "lesson_contents",
        ["content_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_lessons_content_id", "lessons", ["content_id"])

    # Lessons sent before shared content become content of their own, keyed 'legacy': a new
    # recipe never reuses them, but the sent lessons keep pointing at what they showed.
    op.execute(
        """
        INSERT INTO lesson_contents
            (topic_id, cefr, curriculum_version, content_key, status, schema_version, payload,
             attempts, generated_at, created_at)
        SELECT DISTINCT ON (topic_id, cefr, COALESCE(curriculum_version, 0))
            topic_id, cefr, COALESCE(curriculum_version, 0), 'legacy', 'ready', schema_version, payload,
            1, sent_at, created_at
        FROM lessons
        WHERE status = 'sent' AND payload IS NOT NULL
        ORDER BY topic_id, cefr, COALESCE(curriculum_version, 0), sent_at DESC
        """
    )
    op.execute(
        """
        UPDATE lessons SET content_id = lesson_contents.id
        FROM lesson_contents
        WHERE lessons.status = 'sent' AND lessons.payload IS NOT NULL
          AND lesson_contents.content_key = 'legacy'
          AND lesson_contents.topic_id = lessons.topic_id
          AND lesson_contents.cefr = lessons.cefr
          AND lesson_contents.curriculum_version = COALESCE(lessons.curriculum_version, 0)
        """
    )


def downgrade() -> None:
    # lessons.payload is still written next to content_id, so nothing is lost here.
    op.drop_index("ix_lessons_content_id", table_name="lessons")
    op.drop_constraint("fk_lessons_content_id_lesson_contents", "lessons", type_="foreignkey")
    op.drop_column("lessons", "content_id")
    op.drop_table("lesson_contents")
