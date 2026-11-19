"""Project RFI log: house-numbered RFIs, lifecycle, client responses.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

S = db.SCHEMA
T = db.UTCDateTime
J = db.JSONText


def upgrade():
    op.create_table(
        "rfi_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey(db.qualified("projects.id")), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("log_no", sa.Unicode(120), nullable=False),
        sa.Column("run_id", sa.String(32)),
        sa.Column("run_rfi_no", sa.Unicode(40)),
        sa.Column("subject", sa.Unicode(500), nullable=False),
        sa.Column("discipline", sa.Unicode(60)),
        sa.Column("priority", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("content", J),
        sa.Column("finding_ids", J),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("due_date", sa.Unicode(10)),
        sa.Column("assignee_id", sa.String(32)),
        sa.Column("approved_by", sa.String(32)),
        sa.Column("approved_at", T),
        sa.Column("issued_by", sa.String(32)),
        sa.Column("issued_at", T),
        sa.Column("responded_at", T),
        sa.Column("response", sa.UnicodeText),
        sa.Column("response_from", sa.Unicode(200)),
        sa.Column("closed_at", T),
        sa.Column("created_by", sa.String(32)),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
        sa.Column("deleted_at", T),
        sa.UniqueConstraint("project_id", "log_no", name="uq_rfi_log_no"),
        schema=S,
    )
    op.create_index("ix_rfi_log_project", "rfi_log", ["project_id", "seq"], schema=S)
    op.create_table(
        "rfi_log_files",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("log_id", sa.String(32), sa.ForeignKey(db.qualified("rfi_log.id")), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.Unicode(300), nullable=False),
        sa.Column("path", sa.Unicode(500), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("uploaded_by", sa.String(32)),
        sa.Column("created_at", T, nullable=False),
        schema=S,
    )
    op.create_index("ix_rfi_log_files_log", "rfi_log_files", ["log_id"], schema=S)


def downgrade():
    op.drop_table("rfi_log_files", schema=S)
    op.drop_table("rfi_log", schema=S)
