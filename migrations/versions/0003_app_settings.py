"""Settings changed from the admin console (AI fleet profile and role overrides, ...).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

S = db.SCHEMA


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", db.JSONText),
        sa.Column("updated_at", db.UTCDateTime, nullable=False),
        sa.Column("updated_by", sa.String(32)),
        schema=S,
    )


def downgrade():
    op.drop_table("app_settings", schema=S)
