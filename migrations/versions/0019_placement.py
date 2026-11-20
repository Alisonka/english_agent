"""placement test: exams.kind (an exam, or the placement test)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("exams", sa.Column("kind", sa.String(length=16), server_default=sa.text("'exam'"), nullable=False))
    op.create_check_constraint("ck_exams_kind", "exams", "kind IN ('exam', 'placement')")
    op.drop_constraint("uq_exams_learner_id_plan_date", "exams", type_="unique")
    op.create_unique_constraint("uq_exams_learner_id_plan_date_kind", "exams", ["learner_id", "plan_date", "kind"])


def downgrade() -> None:
    op.execute("DELETE FROM exams WHERE kind = 'placement'")
    op.drop_constraint("uq_exams_learner_id_plan_date_kind", "exams", type_="unique")
    op.create_unique_constraint("uq_exams_learner_id_plan_date", "exams", ["learner_id", "plan_date"])
    op.drop_constraint("ck_exams_kind", "exams", type_="check")
    op.drop_column("exams", "kind")
