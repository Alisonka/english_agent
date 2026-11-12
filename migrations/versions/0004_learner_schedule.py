"""learner schedule: per-learner time zone, send times, quiz delay, course start, pause and deactivation

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server defaults equal the old global settings' defaults, so existing rows keep today's schedule.
    op.add_column(
        "learners", sa.Column("timezone", sa.String(length=64), server_default="Europe/Moscow", nullable=False)
    )
    op.add_column(
        "learners", sa.Column("lesson_send_time", sa.Time(), server_default=sa.text("'09:00'"), nullable=False)
    )
    op.add_column(
        "learners", sa.Column("translation_send_time", sa.Time(), server_default=sa.text("'18:00'"), nullable=False)
    )
    op.add_column(
        "learners", sa.Column("quiz_delay_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False)
    )
    op.create_check_constraint("ck_learners_quiz_delay_minutes", "learners", "quiz_delay_minutes >= 0")
    op.add_column("learners", sa.Column("curriculum_started_on", sa.Date(), nullable=True))
    op.add_column("learners", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column("learners", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("learners", sa.Column("deactivated_reason", sa.String(length=64), nullable=True))

    # Every existing learner asked for lessons (/start or /level): they count as started, on the
    # day of their first lesson, or the day they joined if none was sent yet.
    op.execute(
        """
        UPDATE learners SET curriculum_started_on = COALESCE(
            (SELECT min(lessons.lesson_date) FROM lessons WHERE lessons.learner_id = learners.id),
            (learners.created_at AT TIME ZONE learners.timezone)::date
        )
        """
    )


def downgrade() -> None:
    op.drop_column("learners", "deactivated_reason")
    op.drop_column("learners", "paused_at")
    op.drop_column("learners", "is_active")
    op.drop_column("learners", "curriculum_started_on")
    op.drop_constraint("ck_learners_quiz_delay_minutes", "learners", type_="check")
    op.drop_column("learners", "quiz_delay_minutes")
    op.drop_column("learners", "translation_send_time")
    op.drop_column("learners", "lesson_send_time")
    op.drop_column("learners", "timezone")
