"""exercise bank: probes, contrast pairs and drills composed for learners' gaps

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exercise_bank",
        sa.Column("question_key", sa.String(length=240), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("item_key", sa.String(length=200), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("question", JSONB(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('probe', 'contrast_pair', 'drill')", name="ck_exercise_bank_kind"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_exercise_bank_item_id_items", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_exercise_bank_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("question_key", name="pk_exercise_bank"),
    )
    op.create_index("ix_exercise_bank_item_id", "exercise_bank", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_bank_item_id", table_name="exercise_bank")
    op.drop_table("exercise_bank")
