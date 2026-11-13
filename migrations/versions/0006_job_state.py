"""job state: lessons pending/sending/skipped with claims and attempts; translation grading attempts

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_lessons_status", "lessons", type_="check")
    op.create_check_constraint(
        "ck_lessons_status", "lessons", "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')"
    )
    op.add_column("lessons", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lessons", sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False))
    # The planner looks for unfinished rows; sent ones are the vast majority and never match.
    op.create_index(
        "ix_lessons_unfinished",
        "lessons",
        ["lesson_date"],
        postgresql_where=sa.text("status IN ('pending', 'sending', 'failed')"),
    )

    op.add_column(
        "translation_answers", sa.Column("grading_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column("translation_answers", sa.Column("grading_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_translation_answers_pending",
        "translation_answers",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_translation_answers_pending", table_name="translation_answers")
    op.drop_column("translation_answers", "grading_started_at")
    op.drop_column("translation_answers", "grading_attempts")
    op.drop_index("ix_lessons_unfinished", table_name="lessons")
    op.drop_column("lessons", "attempts")
    op.drop_column("lessons", "claimed_at")
    # Rows the old code cannot read: an unfinished day counts as failed, a skipped one too.
    op.execute("UPDATE lessons SET status = 'failed' WHERE status IN ('pending', 'sending', 'skipped')")
    op.drop_constraint("ck_lessons_status", "lessons", type_="check")
    op.create_check_constraint("ck_lessons_status", "lessons", "status IN ('sent', 'failed')")
