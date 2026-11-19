"""content run report: lesson_contents.run

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # How the last generation run of the content went (stages, calls, cost, the Critic's verdicts);
    # NULL for content written before runs were reported.
    op.add_column("lesson_contents", sa.Column("run", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("lesson_contents", "run")
