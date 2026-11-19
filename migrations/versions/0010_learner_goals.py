"""learner goal and interests: learners.goal, learners.interests

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("learners", sa.Column("goal", sa.String(length=16), nullable=True))
    op.create_check_constraint("ck_learners_goal", "learners", "goal IN ('work', 'travel', 'exam', 'general')")
    op.add_column("learners", sa.Column("interests", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column("learners", "interests")
    op.drop_constraint("ck_learners_goal", "learners", type_="check")
    op.drop_column("learners", "goal")
