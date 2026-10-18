"""Whether a signed-in user may still use the portal, and with what rights.

Sessions are stateless cookies, so anything that changes after sign-in is looked up when
the user makes a request (in the portal, or with one of their API keys), and the answer is
kept for a minute per user on each gateway task. Nothing runs in the background: an idle
user costs nothing, and a busy one costs one lookup a minute however many requests they
make. Requests that arrive together while a lookup is under way share it.

* **Disabled** in the gateway (Admin -> Users): refused.
* **Signed out**: sessions issued before the user last signed out are refused, so a
  copied session cookie dies with the one signed out (`sessions_valid_after`).
* **Cognito** (when configured) is asked whether the user still exists and is enabled,
  and, for admin sessions only, whether they are still in the `admin` group. Deleting or
  disabling someone in the Cognito console, or removing them from `admin`, takes effect
  within a minute, not when their session expires. Someone newly added to `admin` gets the
  rights when they next sign in. If Cognito can't be reached, the gateway's own record
  decides access, and admin rights are withheld until Cognito answers again.

Operator sessions (API-key sign-in) end when their key is removed (auth.py).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime

from gateway.accounts.directory import Directory, DirectoryError
from gateway.accounts.store import SqlStore
from gateway.auth import Session

logger = logging.getLogger("gateway.users")

CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class UserState:
    active: bool
    # None: not asked (Cognito not configured, or not an admin session); the session's
    # own flag stands.
    admin: bool | None
    sessions_valid_after: datetime | None = None


INACTIVE = UserState(active=False, admin=False)


class UserGate:
    def __init__(self, store: SqlStore, directory: Directory | None = None) -> None:
        self._store = store
        self._directory = directory
        # sub -> (expires, state, whether admin rights were looked up)
        self._cache: dict[str, tuple[float, UserState, bool]] = {}
        self._pending: dict[tuple[str, bool], asyncio.Future[UserState]] = {}
        self._forgotten: dict[str, int] = {}  # sub -> times forgotten, to drop stale lookups

    async def check(self, session: Session) -> Session | None:
        """The session with its current rights, or None if it may no longer be used."""
        sub = session.user_sub
        if sub is None:
            return session
        state = await self.state(sub, admin=session.admin)
        if not state.active:
            return None
        cutoff = state.sessions_valid_after
        if cutoff is not None and session.issued < cutoff.timestamp():
            return None
        admin = session.admin if state.admin is None else state.admin
        return replace(session, admin=admin)

    async def state(self, sub: str, *, admin: bool = False) -> UserState:
        """The user's state; with `admin`, including whether they are still an admin."""
        cached = self._cache.get(sub)
        if cached is not None and cached[0] > time.monotonic() and (cached[2] or not admin):
            return cached[1]
        key = (sub, admin)
        pending = self._pending.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._refresh(sub, admin))
            self._pending[key] = pending
            pending.add_done_callback(lambda done: self._finished(key, done))
        # Shielded: one request giving up (a client disconnecting) leaves it for the rest.
        return await asyncio.shield(pending)

    def _finished(self, key: tuple[str, bool], done: "asyncio.Future[UserState]") -> None:
        if self._pending.get(key) is done:  # not a newer lookup started after a forget()
            del self._pending[key]

    def forget(self, sub: str) -> None:
        """Apply a change at once on this task (e.g. the admin who disabled the user)."""
        self._cache.pop(sub, None)
        self._forgotten[sub] = self._forgotten.get(sub, 0) + 1
        for key in [(sub, False), (sub, True)]:
            self._pending.pop(key, None)  # a lookup under way may predate the change

    async def _refresh(self, sub: str, admin: bool) -> UserState:
        forgotten = self._forgotten.get(sub, 0)
        state = await self._lookup(sub, admin)
        if self._forgotten.get(sub, 0) == forgotten:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[sub] = (time.monotonic() + CACHE_SECONDS, state, admin)
        return state

    async def _lookup(self, sub: str, admin: bool) -> UserState:
        user = await self._store.get_user(sub)
        if user is None or not user.active:
            return INACTIVE
        if self._directory is None:
            return UserState(True, None, user.sessions_valid_after)
        try:
            found = await self._directory.user_state(sub, groups=admin)
        except DirectoryError as exc:
            logger.warning("could not check the user with Cognito", extra={"reason": str(exc)})
            # No admin until it answers; for a non-admin session, nothing changes.
            return UserState(True, False if admin else None, user.sessions_valid_after)
        if found is None or not found.enabled:
            return INACTIVE  # deleted or disabled in Cognito directly
        return UserState(True, found.admin if admin else None, user.sessions_valid_after)
