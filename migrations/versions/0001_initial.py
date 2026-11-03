"""initial: learners, lessons, items, errors, cards, review_logs

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "learners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=False),
        sa.Column("cefr", sa.String(length=2), nullable=False),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_learners"),
        sa.UniqueConstraint("telegram_chat_id", name="uq_learners_telegram_chat_id"),
    )

    op.create_table(
        "lessons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("lesson_date", sa.Date(), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("cefr", sa.String(length=2), nullable=False),
        sa.Column("curriculum_version", sa.Integer(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("telegram_message_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        _created_at(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('sent', 'failed')", name="ck_lessons_status"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_lessons_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lessons"),
        sa.UniqueConstraint("learner_id", "lesson_date", name="uq_lessons_learner_id_lesson_date"),
    )

    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("cefr", sa.String(length=2), nullable=False),
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("function_ru", sa.Text(), nullable=False),
        sa.Column("explanation_ru", sa.Text(), nullable=False),
        sa.Column("sources", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_lesson_id", sa.Integer(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["source_lesson_id"], ["lessons.id"], name="fk_items_source_lesson_id_lessons", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_items"),
        sa.UniqueConstraint("key", name="uq_items_key"),
    )
    op.create_index("ix_items_topic_id", "items", ["topic_id"])

    op.create_table(
        "errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("corrected", sa.Text(), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("explanation_ru", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint("kind IN ('quiz', 'text')", name="ck_errors_kind"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_errors_item_id_items", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_errors_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_errors"),
    )
    op.create_index("ix_errors_learner_id", "errors", ["learner_id"])

    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("fsrs_state", JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_cards_item_id_items", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_cards_learner_id_learners", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cards"),
        sa.UniqueConstraint("learner_id", "item_id", name="uq_cards_learner_id_item_id"),
    )
    op.create_index("ix_cards_learner_id_due_at", "cards", ["learner_id", "due_at"])

    op.create_table(
        "review_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], name="fk_review_logs_card_id_cards", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_review_logs"),
    )
    op.create_index("ix_review_logs_card_id", "review_logs", ["card_id"])


def downgrade() -> None:
    op.drop_table("review_logs")
    op.drop_table("cards")
    op.drop_table("errors")
    op.drop_table("items")
    op.drop_table("lessons")
    op.drop_table("learners")
