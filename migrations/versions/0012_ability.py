"""ability and difficulty: learner_abilities, question_stats

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learner_abilities",
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("answers", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_learner_abilities_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("learner_id", "scope", name="pk_learner_abilities"),
    )
    op.create_table(
        "question_stats",
        sa.Column("question_key", sa.String(length=240), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("topic_id", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("difficulty", sa.Float(), nullable=False),
        sa.Column("prior", sa.Float(), nullable=False),
        sa.Column("prior_source", sa.String(length=16), nullable=False),
        sa.Column("answers", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_question_stats_item_id_items", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("question_key", name="pk_question_stats"),
    )
    op.create_index("ix_question_stats_item_id", "question_stats", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_question_stats_item_id", table_name="question_stats")
    op.drop_table("question_stats")
    op.drop_table("learner_abilities")
