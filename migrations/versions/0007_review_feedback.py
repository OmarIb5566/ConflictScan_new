"""Reviewer feedback on findings - the decisions later scans learn from.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

S = db.SCHEMA
T = db.UTCDateTime


def upgrade():
    op.create_table(
        "review_feedback",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("finding_id", sa.Unicode(64), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("model_severity", sa.String(20)),
        sa.Column("final_severity", sa.String(20)),
        sa.Column("issue_class", sa.String(60)),
        sa.Column("finding", db.JSONText),
        sa.Column("text", sa.UnicodeText, nullable=False),
        sa.Column("note", sa.UnicodeText),
        sa.Column("decided_by", sa.Unicode(200)),
        sa.Column("embedding", db.JSONText),
        sa.Column("embed_model", sa.Unicode(120)),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
        sa.UniqueConstraint("project_id", "signature", name="uq_review_feedback_finding"),
        schema=S,
    )
    op.create_index("ix_review_feedback_run", "review_feedback", ["run_id", "finding_id"], schema=S)


def downgrade():
    op.drop_table("review_feedback", schema=S)
