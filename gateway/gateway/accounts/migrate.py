"""Run the Alembic migrations (gateway/accounts/migrations) against the store's engine.

Called once per process before the first query. On Postgres a transaction-scoped advisory
lock makes concurrent gateway tasks migrate one at a time; the others then find the
schema already at head. Migrations must therefore stay backwards compatible with the
previous release, which keeps serving during a rolling deploy (add, then remove later).

To add a migration: write gateway/accounts/migrations/versions/vNNNN_<what>.py by hand
(see v0001_api_keys.py for the shape), and update schema.py to match.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_LOCK_ID = 7_402_771  # arbitrary, fixed: "the gateway's migrations"


def _upgrade(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def migrate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        await connection.run_sync(_upgrade)
