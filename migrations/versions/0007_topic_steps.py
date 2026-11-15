"""topic steps: step columns on lessons, payload kind on contents, practice payload and summary kind

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A topic is now taught over several steps. Existing rows are one step ("core") of one, a main
    # variant carrying a whole lesson; the server defaults backfill them without a data migration.
    op.add_column("lessons", sa.Column("step_index", sa.SmallInteger(), server_default=sa.text("1"), nullable=False))
    op.add_column("lessons", sa.Column("step_count", sa.SmallInteger(), server_default=sa.text("1"), nullable=False))
    op.add_column(
        "lessons", sa.Column("step_slug", sa.String(length=40), server_default=sa.text("'core'"), nullable=False)
    )
    op.add_column(
        "lessons", sa.Column("variant", sa.String(length=8), server_default=sa.text("'main'"), nullable=False)
    )
    op.add_column(
        "lessons", sa.Column("payload_kind", sa.String(length=16), server_default=sa.text("'lesson'"), nullable=False)
    )
    op.create_check_constraint("ck_lessons_variant", "lessons", "variant IN ('main', 'alt')")
    op.create_check_constraint("ck_lessons_payload_kind", "lessons", "payload_kind IN ('lesson', 'step')")
    op.create_index("ix_lessons_learner_id_topic_id", "lessons", ["learner_id", "topic_id"])

    op.add_column(
        "lesson_contents",
        sa.Column("payload_kind", sa.String(length=16), server_default=sa.text("'lesson'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_lesson_contents_payload_kind", "lesson_contents", "payload_kind IN ('lesson', 'topic')"
    )

    # The day's assembled practice set (which items each question/sentence checks); NULL for legacy rows.
    op.add_column("practice_deliveries", sa.Column("payload", JSONB(), nullable=True))
    op.drop_constraint("ck_practice_deliveries_kind", "practice_deliveries", type_="check")
    op.create_check_constraint(
        "ck_practice_deliveries_kind", "practice_deliveries", "kind IN ('quiz', 'translation', 'summary')"
    )


def downgrade() -> None:
    # Cheat-sheet deliveries have no place in the old schema.
    op.execute("DELETE FROM practice_deliveries WHERE kind = 'summary'")
    op.drop_constraint("ck_practice_deliveries_kind", "practice_deliveries", type_="check")
    op.create_check_constraint("ck_practice_deliveries_kind", "practice_deliveries", "kind IN ('quiz', 'translation')")
    op.drop_column("practice_deliveries", "payload")

    op.drop_constraint("ck_lesson_contents_payload_kind", "lesson_contents", type_="check")
    op.drop_column("lesson_contents", "payload_kind")

    op.drop_index("ix_lessons_learner_id_topic_id", table_name="lessons")
    op.drop_constraint("ck_lessons_payload_kind", "lessons", type_="check")
    op.drop_constraint("ck_lessons_variant", "lessons", type_="check")
    op.drop_column("lessons", "payload_kind")
    op.drop_column("lessons", "variant")
    op.drop_column("lessons", "step_slug")
    op.drop_column("lessons", "step_count")
    op.drop_column("lessons", "step_index")
