"""learner model: learner_profiles, quiz_polls.item_id, review_logs.source_ref, card review counters

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learner_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_learner_profiles_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learner_profiles"),
        sa.UniqueConstraint("learner_id", "version", name="uq_learner_profiles_learner_id_version"),
    )

    op.add_column("quiz_polls", sa.Column("item_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_quiz_polls_item_id_items", "quiz_polls", "items", ["item_id"], ["id"], ondelete="SET NULL"
    )

    # review_logs has never been written to, so the new NOT NULL column needs no default.
    op.add_column("review_logs", sa.Column("source_ref", sa.String(length=64), nullable=False))
    op.create_unique_constraint("uq_review_logs_source_ref", "review_logs", ["source_ref"])

    op.add_column("cards", sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cards", sa.Column("reviews", sa.Integer(), server_default=sa.text("0"), nullable=False))

    op.drop_index("ix_errors_learner_id", table_name="errors")
    op.create_index("ix_errors_learner_id_created_at", "errors", ["learner_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_errors_learner_id_created_at", table_name="errors")
    op.create_index("ix_errors_learner_id", "errors", ["learner_id"])

    op.drop_column("cards", "reviews")
    op.drop_column("cards", "last_reviewed_at")

    op.drop_constraint("uq_review_logs_source_ref", "review_logs", type_="unique")
    op.drop_column("review_logs", "source_ref")

    op.drop_constraint("fk_quiz_polls_item_id_items", "quiz_polls", type_="foreignkey")
    op.drop_column("quiz_polls", "item_id")

    op.drop_table("learner_profiles")
