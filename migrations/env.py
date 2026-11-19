"""Alembic environment: the URL and schema come from db.py (DATABASE_URL / settings file)."""
import os
import sys

from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

target_metadata = db.metadata


def _schema_ddl(dialect_name):
    """CREATE SCHEMA before Alembic creates its version table inside it."""
    s = db.SCHEMA
    if not s:
        return None
    if dialect_name == "postgresql":
        return f'CREATE SCHEMA IF NOT EXISTS "{s}"'
    if dialect_name == "mssql":
        return f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{s}') EXEC('CREATE SCHEMA [{s}]')"
    return None


def run_offline():
    """`python manage.py sql`: write the migrations as plain SQL for a DBA."""
    context.configure(url=db.DATABASE_URL, target_metadata=target_metadata, literal_binds=True,
                      version_table_schema=db.SCHEMA, include_schemas=bool(db.SCHEMA))
    with context.begin_transaction():
        ddl = _schema_ddl(context.get_context().dialect.name)
        if ddl:
            context.execute(ddl)
        context.run_migrations()


def run_online():
    connectable = context.config.attributes.get("connection")
    if connectable is None:
        with db.engine.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection):
    ddl = _schema_ddl(connection.dialect.name)
    if ddl:
        from sqlalchemy import text
        connection.execute(text(ddl))
    context.configure(connection=connection, target_metadata=target_metadata,
                      version_table_schema=db.SCHEMA, include_schemas=bool(db.SCHEMA),
                      render_as_batch=db.IS_SQLITE)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
