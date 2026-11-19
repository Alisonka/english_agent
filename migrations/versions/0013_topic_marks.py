"""topic marks: topics credited by the placement test or confirmed by an exam

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_marks",
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("mark", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=64), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mark IN ('credited', 'confirmed')", name="ck_topic_marks_mark"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_topic_marks_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("learner_id", "topic_id", "mark", name="pk_topic_marks"),
    )


def downgrade() -> None:
    op.drop_table("topic_marks")
