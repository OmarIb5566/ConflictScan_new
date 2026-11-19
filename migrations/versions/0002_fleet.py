"""AI fleet: response cache, per-call log, RFI memory.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

S = db.SCHEMA
T = db.UTCDateTime
J = db.JSONText


def upgrade():
    op.create_table(
        "ai_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("model", sa.Unicode(120), nullable=False),
        sa.Column("value", sa.UnicodeText, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("last_used_at", T, nullable=False),
        sa.Column("hits", sa.Integer, nullable=False),
        schema=S,
    )
    op.create_index("ix_ai_cache_used", "ai_cache", ["last_used_at"], schema=S)
    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("at", T, nullable=False),
        sa.Column("run_id", sa.String(32)),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("model", sa.Unicode(120), nullable=False),
        sa.Column("server", sa.Unicode(200)),
        sa.Column("ms", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("images", sa.Integer, nullable=False),
        sa.Column("cached", sa.Boolean, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Unicode(300)),
        schema=S,
    )
    op.create_index("ix_model_calls_run", "model_calls", ["run_id"], schema=S)
    op.create_index("ix_model_calls_at", "model_calls", ["at"], schema=S)
    op.create_table(
        "rfi_memory",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("project_name", sa.Unicode(200), nullable=False),
        sa.Column("rfi_no", sa.Unicode(120), nullable=False),
        sa.Column("subject", sa.Unicode(500), nullable=False),
        sa.Column("text", sa.UnicodeText, nullable=False),
        sa.Column("discipline", sa.Unicode(60)),
        sa.Column("issue_class", sa.String(60)),
        sa.Column("response", sa.UnicodeText),
        sa.Column("embedding", J),
        sa.Column("embed_model", sa.Unicode(120)),
        sa.Column("created_at", T, nullable=False),
        sa.UniqueConstraint("source", "project_name", "rfi_no", name="uq_rfi_memory_item"),
        schema=S,
    )


def downgrade():
    for name in ("rfi_memory", "model_calls", "ai_cache"):
        op.drop_table(name, schema=S)
