"""evidence: one table of learner events, backfilled from quiz answers, graded translations and disputes

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("quiz_answer", "translation_sentence", "exam_answer", "probe", "tutor_question", "missed_day", "dispute")

# The topic of an event is its item's topic (practice is cumulative), else the lesson's.
_QUIZ = """
INSERT INTO evidence (learner_id, kind, source_ref, topic_id, item_id, payload, occurred_at)
SELECT a.learner_id, 'quiz_answer', 'quiz_answer:' || a.id, COALESCE(i.topic_id, l.topic_id), p.item_id,
       jsonb_build_object('poll_id', p.id, 'is_correct', a.is_correct), a.answered_at
FROM quiz_answers a
JOIN quiz_polls p ON p.id = a.poll_id
JOIN practice_deliveries d ON d.id = p.delivery_id
JOIN lessons l ON l.id = d.lesson_id
LEFT JOIN items i ON i.id = p.item_id
ON CONFLICT (source_ref) DO NOTHING
"""

# Step days name each sentence's item in the delivery payload; legacy days rate the topic's core item.
_TRANSLATION = """
INSERT INTO evidence (learner_id, kind, source_ref, topic_id, item_id, payload, occurred_at)
SELECT t.learner_id, 'translation_sentence', 'translation_answer:' || t.id || ':' || (s ->> 'n'),
       COALESCE(i.topic_id, l.topic_id), i.id,
       jsonb_build_object('verdict', s ->> 'verdict', 'error_type', s -> 'error_type'),
       COALESCE(t.graded_at, t.created_at)
FROM translation_answers t
JOIN practice_deliveries d ON d.id = t.delivery_id
JOIN lessons l ON l.id = d.lesson_id
CROSS JOIN LATERAL jsonb_array_elements(t.grade -> 'sentences') AS s
LEFT JOIN items i ON i.id = CASE
    WHEN d.payload IS NOT NULL THEN (d.payload -> 'sentences' -> ((s ->> 'n')::int - 1) ->> 'item_id')::int
    ELSE (SELECT core.id FROM items core WHERE core.key = l.topic_id || ':' || 'core')
END
WHERE t.status = 'graded' AND t.grade IS NOT NULL
ON CONFLICT (source_ref) DO NOTHING
"""

_DISPUTES = """
INSERT INTO evidence (learner_id, kind, source_ref, topic_id, payload, occurred_at)
SELECT t.learner_id, 'dispute', 'dispute:translation_answer:' || t.id, l.topic_id,
       jsonb_build_object('translation_answer_id', t.id), COALESCE(t.graded_at, t.created_at)
FROM translation_answers t
JOIN practice_deliveries d ON d.id = t.delivery_id
JOIN lessons l ON l.id = d.lesson_id
WHERE t.disputed
ON CONFLICT (source_ref) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("learner_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=64), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("error_id", sa.Integer(), nullable=True),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"kind IN ({', '.join(repr(kind) for kind in KINDS)})", name="ck_evidence_kind"),
        sa.ForeignKeyConstraint(
            ["learner_id"], ["learners.id"], name="fk_evidence_learner_id_learners", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], name="fk_evidence_item_id_items", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["error_id"], ["errors.id"], name="fk_evidence_error_id_errors", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
        sa.UniqueConstraint("source_ref", name="uq_evidence_source_ref"),
    )
    op.create_index("ix_evidence_learner_id_occurred_at", "evidence", ["learner_id", "occurred_at"])
    op.create_index("ix_evidence_learner_id_topic_id", "evidence", ["learner_id", "topic_id"])

    # History before this table: errors are not linked (an old error row names no answer).
    op.execute(_QUIZ)
    op.execute(_TRANSLATION)
    op.execute(_DISPUTES)


def downgrade() -> None:
    op.drop_index("ix_evidence_learner_id_topic_id", table_name="evidence")
    op.drop_index("ix_evidence_learner_id_occurred_at", table_name="evidence")
    op.drop_table("evidence")
