"""A per-client limit on the portal's unauthenticated forms: key sign-in, and joining
with an invite code.

API keys are at least 32 random characters, so for sign-in this only stops one client
hammering the key check. Invite codes are shorter so they can be typed (about 59 bits,
12 characters from 31): here the limit is part of what makes guessing hopeless, together
with codes expiring and working once. It also stops a client hammering Cognito through
account creation. In memory per gateway task: with one task that is the whole limit;
with more, each task allows its own share.
"""

import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, max_clients: int = 10_000) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_clients = max_clients
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, client: str, now: float | None = None) -> int | None:
        """Record an attempt. None if it is allowed; else the seconds until the next one is."""
        now = time.monotonic() if now is None else now
        hits = self._hits.get(client)
        if hits is None:
            if len(self._hits) >= self._max_clients:
                self._forget_idle(now)
            hits = self._hits[client] = deque()
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, int(hits[0] + self._window - now) + 1)
        hits.append(now)
        return None

    def _forget_idle(self, now: float) -> None:
        for client in [c for c, h in self._hits.items() if not h or h[-1] <= now - self._window]:
            del self._hits[client]
        if len(self._hits) >= self._max_clients:  # all active: start again rather than grow
            self._hits.clear()
