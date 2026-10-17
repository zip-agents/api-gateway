"""Alembic environment: run against the connection migrate.py passes in (never a URL)."""

from alembic import context

from gateway.accounts.schema import metadata

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=metadata,
    # SQLite (tests) cannot ALTER most things in place; batch mode rebuilds tables instead.
    render_as_batch=connection.dialect.name == "sqlite",
)
with context.begin_transaction():
    context.run_migrations()
