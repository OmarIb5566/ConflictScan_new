"""Revisions: document lineage across revisions and superseded versions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

S = db.SCHEMA


def upgrade():
    with op.batch_alter_table("run_files", schema=S) as b:
        b.add_column(sa.Column("lineage_id", sa.String(32)))
        b.add_column(sa.Column("supersedes", sa.Unicode(300)))
        b.add_column(sa.Column("supersedes_sha256", sa.String(64)))
    op.create_index("ix_run_files_lineage", "run_files", ["lineage_id"], schema=S)
    # Existing files: each one starts its own lineage.
    op.execute(sa.text(f"UPDATE {db.qualified('run_files')} SET lineage_id = id WHERE lineage_id IS NULL"))


def downgrade():
    op.drop_index("ix_run_files_lineage", "run_files", schema=S)
    with op.batch_alter_table("run_files", schema=S) as b:
        b.drop_column("supersedes_sha256")
        b.drop_column("supersedes")
        b.drop_column("lineage_id")
