"""content orders: remedial and contrast lessons the Mentor ordered

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("cefr", sa.String(length=2), nullable=False),
        sa.Column("brief", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('ordered', 'ready', 'failed')", name="ck_content_orders_status"),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["lesson_contents.id"],
            name="fk_content_orders_content_id_lesson_contents",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_content_orders"),
        sa.UniqueConstraint("topic_id", "cefr", name="uq_content_orders_topic_id_cefr"),
    )
    op.create_index(
        "ix_content_orders_open", "content_orders", ["created_at"], postgresql_where=sa.text("status = 'ordered'")
    )


def downgrade() -> None:
    op.drop_index("ix_content_orders_open", table_name="content_orders")
    op.drop_table("content_orders")
