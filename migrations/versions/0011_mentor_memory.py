"""Mentor memory: journal_entries, hypotheses, dossiers

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _learner(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["learner_id"], ["learners.id"], name=f"fk_{table}_learner_id_learners", ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_ids", JSONB(), nullable=False),
        sa.Column("expectation", JSONB(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("outcome IN ('met', 'not_met', 'insufficient')", name="ck_journal_entries_outcome"),
        _learner("journal_entries"),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entries"),
    )
    op.create_index("ix_journal_entries_learner_id_created_at", "journal_entries", ["learner_id", "created_at"])
    op.create_index(
        "ix_journal_entries_unchecked",
        "journal_entries",
        ["due_on"],
        postgresql_where=sa.text("outcome IS NULL AND due_on IS NOT NULL"),
    )

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("statement_ru", sa.Text(), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=True),
        sa.Column("item_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("evidence_ids", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evidence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'probing', 'confirmed', 'refuted', 'stale')", name="ck_hypotheses_status"
        ),
        _learner("hypotheses"),
        sa.PrimaryKeyConstraint("id", name="pk_hypotheses"),
    )
    op.create_index("ix_hypotheses_learner_id_status", "hypotheses", ["learner_id", "status"])

    op.create_table(
        "dossiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("until_entry_id", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _learner("dossiers"),
        sa.PrimaryKeyConstraint("id", name="pk_dossiers"),
    )
    op.create_index("ix_dossiers_learner_id_created_at", "dossiers", ["learner_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dossiers_learner_id_created_at", table_name="dossiers")
    op.drop_table("dossiers")
    op.drop_index("ix_hypotheses_learner_id_status", table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_index("ix_journal_entries_unchecked", table_name="journal_entries")
    op.drop_index("ix_journal_entries_learner_id_created_at", table_name="journal_entries")
    op.drop_table("journal_entries")
