"""practice: practice_deliveries, quiz_polls, quiz_answers, translation_answers

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "practice_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        _now("claimed_at"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('quiz', 'translation')", name="ck_practice_deliveries_kind"),
        sa.ForeignKeyConstraint(
            ["lesson_id"], ["lessons.id"], name="fk_practice_deliveries_lesson_id_lessons", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_deliveries"),
        sa.UniqueConstraint("lesson_id", "kind", name="uq_practice_deliveries_lesson_id_kind"),
    )

    op.create_table(
        "quiz_polls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("telegram_poll_id", sa.String(length=64), nullable=False),
        sa.Column("telegram_message_id", sa.Integer(), nullable=False),
        sa.Column("question", JSONB(), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["practice_deliveries.id"],
            name="fk_quiz_polls_delivery_id_practice_deliveries",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quiz_polls"),
        sa.UniqueConstraint("telegram_poll_id", name="uq_quiz_polls_telegram_poll_id"),
    )
    op.create_index("ix_quiz_polls_delivery_id", "quiz_polls", ["delivery_id"])

    op.create_table(
        "quiz_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("poll_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("option_ids", JSONB(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        _now("answered_at"),
        sa.ForeignKeyConstraint(
            ["poll_id"], ["quiz_polls.id"], name="fk_quiz_answers_poll_id_quiz_polls", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_quiz_answers_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quiz_answers"),
        sa.UniqueConstraint("poll_id", "learner_id", name="uq_quiz_answers_poll_id_learner_id"),
    )

    op.create_table(
        "translation_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("grade", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("progress_message_id", sa.Integer(), nullable=True),
        sa.Column("disputed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _now("created_at"),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'graded', 'failed')", name="ck_translation_answers_status"),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["practice_deliveries.id"],
            name="fk_translation_answers_delivery_id_practice_deliveries",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_translation_answers_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_translation_answers"),
        sa.UniqueConstraint("delivery_id", name="uq_translation_answers_delivery_id"),
    )


def downgrade() -> None:
    op.drop_table("translation_answers")
    op.drop_table("quiz_answers")
    op.drop_index("ix_quiz_polls_delivery_id", table_name="quiz_polls")
    op.drop_table("quiz_polls")
    op.drop_table("practice_deliveries")
