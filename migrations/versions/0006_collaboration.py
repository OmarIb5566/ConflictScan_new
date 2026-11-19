"""Collaboration: comments and mentions, notifications, resumable upload sessions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

S = db.SCHEMA
T = db.UTCDateTime


def upgrade():
    op.create_table(
        "comments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32)),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Unicode(64), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("body", sa.UnicodeText, nullable=False),
        sa.Column("mentions", db.JSONText),
        sa.Column("created_at", T, nullable=False),
        sa.Column("edited_at", T),
        sa.Column("deleted_at", T),
        schema=S,
    )
    op.create_index("ix_comments_target", "comments", ["project_id", "target_type", "target_id"], schema=S)
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.Unicode(300), nullable=False),
        sa.Column("body", sa.Unicode(1000)),
        sa.Column("link", sa.Unicode(300)),
        sa.Column("project_id", sa.String(32)),
        sa.Column("dedupe", sa.Unicode(120)),
        sa.Column("created_at", T, nullable=False),
        sa.Column("read_at", T),
        schema=S,
    )
    op.create_index("ix_notifications_user", "notifications", ["user_id", "read_at", "created_at"], schema=S)
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("slot", sa.String(4), nullable=False),
        sa.Column("name", sa.Unicode(300), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("received", sa.BigInteger, nullable=False),
        sa.Column("key", sa.Unicode(500), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", T, nullable=False),
        schema=S,
    )


def downgrade():
    for name in ("upload_sessions", "notifications", "comments"):
        op.drop_table(name, schema=S)
