"""A daily byte quota per user: uploads plus archives, through the API and the browser page.

Each user (`user:<cognito sub>`) may move GATEWAY_USER_DAILY_QUOTA_MIB per UTC day across
all their keys and browser uploads. Operator keys are exempt: they are automation (smoke
tests) and have no owner to charge. It is a cost control: data transfer out is the one
AWS cost that grows with use.

* **Checked when a request starts.** A user over quota gets `429 quota-exceeded` with
  `Retry-After` until midnight UTC. A request already running is never cut off, so the
  overrun is at most what the user's requests in flight move.
* **Counted when a request ends:** in memory, written to `usage_daily` about once a
  minute, like key usage. A user's total is read from the database at most once a minute
  per task, plus what this task has not written yet. With one gateway task the count is
  exact; with several, each sees the others' traffic up to a minute late.
* **Fails open.** If the database cannot be read, the request goes ahead (and is logged):
  a paused or failing database must not take the API down for a cost control.
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from gateway.accounts.store import SqlStore

logger = logging.getLogger("gateway.quota")

MIB = 1024 * 1024
FLUSH_SECONDS = 60.0
CACHE_SECONDS = 60.0


def utc_today() -> date:
    return datetime.now(UTC).date()


def seconds_until_reset(now: datetime | None = None) -> int:
    """Seconds until the next midnight UTC (at least 1)."""
    now = now or datetime.now(UTC)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return max(1, int((midnight - now).total_seconds()))


def human_bytes(count: int) -> str:
    """`1.5 GiB`, `320 MiB`, `12 KiB`."""
    for unit, size in (("GiB", 1024 * MIB), ("MiB", MIB), ("KiB", 1024)):
        if count >= size:
            value = count / size
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{count} B"


class QuotaService:
    def __init__(self, store: SqlStore, daily_bytes: int) -> None:
        self._store = store
        self._daily = daily_bytes  # 0: no quota
        self._pending: Counter[tuple[str, date]] = Counter()
        self._requests: Counter[tuple[str, date]] = Counter()
        self._stored: dict[str, tuple[float, date, int]] = {}  # owner -> (expires, day, bytes)
        self._flusher: asyncio.Task[None] | None = None
        self._pruned: date | None = None

    @property
    def daily_bytes(self) -> int:
        return self._daily

    def applies(self, owner: str) -> bool:
        return self._daily > 0 and owner.startswith("user:")

    async def used_today(self, owner: str) -> int:
        """Bytes `owner` has moved today: the stored total plus this task's unwritten count."""
        today = utc_today()
        cached = self._stored.get(owner)
        if cached is not None and cached[0] > time.monotonic() and cached[1] == today:
            stored = cached[2]
        else:
            stored = await self._store.daily_usage(owner, today)
            if len(self._stored) > 4096:
                self._stored.clear()
            self._stored[owner] = (time.monotonic() + CACHE_SECONDS, today, stored)
        return stored + self._pending[(owner, today)]

    async def refusal(self, owner: str) -> tuple[int, str] | None:
        """(Retry-After seconds, message) if `owner` is over quota; None to go ahead."""
        if not self.applies(owner):
            return None
        try:
            used = await self.used_today(owner)
        except Exception:
            logger.exception("could not read quota usage; allowing the request")
            return None
        if used < self._daily:
            return None
        return seconds_until_reset(), (
            f"You have used {human_bytes(used)} of your {human_bytes(self._daily)} daily "
            "allowance (uploads plus archives). It resets at 00:00 UTC."
        )

    def record(self, owner: str, nbytes: int) -> None:
        """Count a finished request's bytes against `owner`."""
        if not self.applies(owner) or nbytes <= 0:
            return
        key = (owner, utc_today())
        self._pending[key] += nbytes
        self._requests[key] += 1
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush_later())

    async def flush(self) -> None:
        """Write the counted bytes: one upsert per user and day."""
        pending, self._pending = self._pending, Counter()
        requests, self._requests = self._requests, Counter()
        for (owner, day), nbytes in pending.items():
            try:
                await self._store.add_daily_usage(owner, day, nbytes, requests[(owner, day)])
            except Exception:
                logger.exception("could not record usage; keeping it for the next flush")
                self._pending[(owner, day)] += nbytes
                self._requests[(owner, day)] += requests[(owner, day)]
                continue
            self._stored.pop(owner, None)  # reread, now including these bytes
        today = utc_today()
        if self._pruned != today:
            try:
                await self._store.prune_daily_usage(today)
                self._pruned = today
            except Exception:
                logger.exception("could not prune old usage")

    async def usage_all(self) -> dict[str, int]:
        """owner -> bytes today, including this task's unwritten counts (admin pages)."""
        today = utc_today()
        usage = Counter(await self._store.daily_usage_all(today))
        for (owner, day), nbytes in self._pending.items():
            if day == today:
                usage[owner] += nbytes
        return dict(usage)

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_SECONDS)
        await self.flush()
