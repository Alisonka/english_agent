"""exams: adaptive exams on a block of topics and their polls

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("topic_ids", JSONB(), nullable=False),
        sa.Column("ability_start", sa.Float(), nullable=False),
        sa.Column("ability", sa.Float(), nullable=False),
        sa.Column("pool", JSONB(), nullable=False),
        sa.Column("answers", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("report", JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('running', 'finished')", name="ck_exams_state"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_exams_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exams"),
        sa.UniqueConstraint("learner_id", "plan_date", name="uq_exams_learner_id_plan_date"),
    )
    op.create_table(
        "exam_polls",
        sa.Column("telegram_poll_id", sa.String(length=64), nullable=False),
        sa.Column("exam_id", sa.Integer(), nullable=False),
        sa.Column("question_key", sa.String(length=240), nullable=False),
        sa.Column("answered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], name="fk_exam_polls_exam_id_exams", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("telegram_poll_id", name="pk_exam_polls"),
    )
    op.create_index("ix_exam_polls_exam_id", "exam_polls", ["exam_id"])


def downgrade() -> None:
    op.drop_index("ix_exam_polls_exam_id", table_name="exam_polls")
    op.drop_table("exam_polls")
    op.drop_table("exams")
