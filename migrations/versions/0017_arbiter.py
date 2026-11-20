"""Arbiter: errors.cancelled_at, translation_answers.arbiter

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("errors", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("translation_answers", sa.Column("arbiter", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("translation_answers", "arbiter")
    op.drop_column("errors", "cancelled_at")
