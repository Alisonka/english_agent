"""day plans: one committed plan per learner and day

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "day_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("plan", JSONB(), nullable=True),
        sa.Column("trajectory", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('pending', 'running', 'committed', 'fallback')", name="ck_day_plans_state"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_day_plans_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_day_plans"),
        sa.UniqueConstraint("learner_id", "plan_date", name="uq_day_plans_learner_id_plan_date"),
    )


def downgrade() -> None:
    op.drop_table("day_plans")
