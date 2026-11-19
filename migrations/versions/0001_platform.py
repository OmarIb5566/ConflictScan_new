"""Platform foundation: users and roles, projects and members, revisions, files,
job queue, audit log, sign-in attempts, presence.

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

import db

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = db.SCHEMA
T = db.UTCDateTime
J = db.JSONText


def _fk(target):
    return sa.ForeignKey(db.qualified(target))


def _ensure_schema():
    if not S:
        return
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{S}"')
    elif dialect == "mssql":
        op.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{S}') EXEC('CREATE SCHEMA [{S}]')")
    # MySQL/Oracle: the schema is the database/user the DBA provides.


def upgrade():
    _ensure_schema()
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.Unicode(80), nullable=False),
        sa.Column("username_key", sa.Unicode(80), nullable=False, unique=True),
        sa.Column("display_name", sa.Unicode(120), nullable=False),
        sa.Column("email", sa.Unicode(200)),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("auth_source", sa.String(20), nullable=False),
        sa.Column("external_id", sa.Unicode(255)),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("must_change_password", sa.Boolean, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("last_seen_at", T),
        schema=S,
    )
    op.create_index("ix_users_external", "users", ["auth_source", "external_id"], schema=S)
    op.create_table(
        "user_prefs",
        sa.Column("user_id", sa.String(32), _fk("users.id"), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", J),
        sa.PrimaryKeyConstraint("user_id", "key"),
        schema=S,
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.Unicode(200), nullable=False),
        sa.Column("code", sa.Unicode(80)),
        sa.Column("description", sa.UnicodeText, nullable=False),
        sa.Column("owner_id", sa.String(32), _fk("users.id"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("archived", sa.Boolean, nullable=False),
        sa.Column("settings", J),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
        sa.Column("deleted_at", T),
        sa.Column("deleted_by", sa.String(32)),
        schema=S,
    )
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.String(32), _fk("projects.id"), nullable=False),
        sa.Column("user_id", sa.String(32), _fk("users.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("added_by", sa.String(32)),
        sa.Column("added_at", T, nullable=False),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
        schema=S,
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), _fk("projects.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("title", sa.Unicode(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(32), _fk("users.id")),
        sa.Column("updated_by", sa.String(32), _fk("users.id")),
        sa.Column("parent_run_id", sa.String(32)),
        sa.Column("inputs", J),
        sa.Column("results", J),
        sa.Column("review", J),
        sa.Column("progress", J),
        sa.Column("error", sa.UnicodeText),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
        sa.Column("queued_at", T),
        sa.Column("started_at", T),
        sa.Column("finished_at", T),
        sa.Column("deleted_at", T),
        sa.Column("deleted_by", sa.String(32)),
        sa.UniqueConstraint("project_id", "seq", name="uq_runs_project_seq"),
        schema=S,
    )
    op.create_index("ix_runs_status", "runs", ["status"], schema=S)
    op.create_table(
        "run_files",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), _fk("runs.id"), nullable=False),
        sa.Column("slot", sa.String(4), nullable=False),
        sa.Column("name", sa.Unicode(300), nullable=False),
        sa.Column("path", sa.Unicode(500), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("detected_kind", sa.String(20)),
        sa.Column("detected_reason", sa.Unicode(500)),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("pages", sa.Integer),
        sa.Column("problem", sa.Unicode(500)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("inspected_at", T),
        sa.Column("created_at", T, nullable=False),
        schema=S,
    )
    op.create_index("ix_run_files_run", "run_files", ["run_id", "slot", "position"], schema=S)
    op.create_index("ix_run_files_sha", "run_files", ["sha256"], schema=S)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("queue_order", sa.BigInteger, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("cancel_requested", sa.Boolean, nullable=False),
        sa.Column("run_after", T, nullable=False),
        sa.Column("locked_by", sa.Unicode(120)),
        sa.Column("locked_at", T),
        sa.Column("heartbeat_at", T),
        sa.Column("error", sa.UnicodeText),
        sa.Column("created_by", sa.String(32)),
        sa.Column("created_at", T, nullable=False),
        sa.Column("finished_at", T),
        schema=S,
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "kind", "priority", "run_after"], schema=S)
    op.create_index("ix_jobs_target", "jobs", ["target_id"], schema=S)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("at", T, nullable=False),
        sa.Column("user_id", sa.String(32)),
        sa.Column("username", sa.Unicode(80)),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(30)),
        sa.Column("target_id", sa.String(64)),
        sa.Column("project_id", sa.String(32)),
        sa.Column("summary", sa.Unicode(500)),
        sa.Column("details", J),
        sa.Column("ip", sa.String(64)),
        schema=S,
    )
    op.create_index("ix_audit_at", "audit_log", ["at"], schema=S)
    op.create_index("ix_audit_project", "audit_log", ["project_id", "at"], schema=S)
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("key", sa.Unicode(200), nullable=False),
        sa.Column("at", T, nullable=False),
        schema=S,
    )
    op.create_index("ix_login_attempts_key", "login_attempts", ["key", "at"], schema=S)
    op.create_table(
        "presence",
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("detail", sa.Unicode(120)),
        sa.Column("at", T, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "target"),
        schema=S,
    )


def downgrade():
    for name in ("presence", "login_attempts", "audit_log", "jobs", "run_files", "runs",
                 "project_members", "projects", "user_prefs", "users"):
        op.drop_table(name, schema=S)
