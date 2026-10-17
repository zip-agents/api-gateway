"""Whether a signed-in user may still use the portal.

Sessions are stateless cookies, so disabling a user must be checked on each request. The
answer is cached briefly per gateway task, so the database is read about once a minute
per active user, not on every page.
"""

import time

from gateway.accounts.store import SqlStore
from gateway.auth import Session

CACHE_SECONDS = 60.0


class UserGate:
    def __init__(self, store: SqlStore) -> None:
        self._store = store
        self._cache: dict[str, tuple[float, bool]] = {}

    async def allowed(self, session: Session) -> bool:
        sub = session.user_sub
        if sub is None:
            return True  # operator sessions end when their key is removed (auth.py)
        cached = self._cache.get(sub)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        user = await self._store.get_user(sub)
        allowed = user is not None and user.active
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[sub] = (time.monotonic() + CACHE_SECONDS, allowed)
        return allowed

    def forget(self, sub: str) -> None:
        """Apply a change at once on this task (e.g. the admin who disabled the user)."""
        self._cache.pop(sub, None)
