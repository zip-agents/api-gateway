"""Opening a database transaction, with one retry while Aurora resumes.

A paused Aurora Serverless v2 instance resumes on the first connection: about 15 s, and
30 s or more once it has been paused for a day, and it may refuse connections ("the
database system is starting up") while it does. AWS recommends retrying. So if opening
the connection fails, wait and try once more. Never after a statement has run: only
entering the transaction is retried, so nothing can be applied twice.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger("gateway.accounts")

CONNECT_RETRY_SECONDS = 2.0
# OperationalError wraps asyncpg's CannotConnectNowError (SQLSTATE 57P03); a refused or
# timed-out connect surfaces as OSError or TimeoutError.
_CONNECT_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError)


@contextlib.asynccontextmanager
async def begin(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """`engine.begin()`, retrying once if the connection can't be opened."""
    async with contextlib.AsyncExitStack() as stack:
        try:
            connection = await stack.enter_async_context(engine.begin())
        except _CONNECT_ERRORS as exc:
            logger.warning(
                "database connection failed, retrying once",
                extra={"fields": {"error": type(exc).__name__}},
            )
            await asyncio.sleep(CONNECT_RETRY_SECONDS)
            connection = await stack.enter_async_context(engine.begin())
        yield connection
