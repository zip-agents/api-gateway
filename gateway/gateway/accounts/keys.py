"""User API keys: creation, verification, revocation and usage counting.

A key looks like `zk_3f9a1c2b_<43 characters>`:

    zk_        marks it as a gateway user key (easy to spot, e.g. by secret scanners)
    3f9a1c2b   the prefix: random, not secret; the key's ID in storage, logs and pages
    the rest   the secret: 32 random bytes, base64url

Only SHA-256(secret) is stored. The full key exists only in the response that creates it.
A fast hash is right here: the secret is 256 random bits, so a stolen hash cannot be
guessed back, and a deliberately slow hash would add latency to every API request.

Verification results are cached for CACHE_SECONDS, so normal traffic does not read the
store per request; revocation therefore takes effect within that window. Usage is counted
in memory and written in one update per key every FLUSH_SECONDS.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import string
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from gateway.accounts.store import ApiKeyRecord, KeyExists, SqlStore

logger = logging.getLogger("gateway.keys")

KEY_PATTERN = re.compile(r"^zk_([a-z0-9]{8})_([A-Za-z0-9_-]{43})$")
NAME_PATTERN = re.compile(r"^[\w .,:()/@+-]{1,64}$")
MAX_KEYS_PER_OWNER = 5
CACHE_SECONDS = 60.0
FLUSH_SECONDS = 60.0
_CACHE_LIMIT = 4096
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits


class TooManyKeys(Exception):
    pass


class InvalidKeyName(ValueError):
    pass


@dataclass(frozen=True)
class KeyPrincipal:
    """Who a verified user key belongs to."""

    prefix: str
    owner: str
    owner_label: str
    name: str


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class KeyService:
    def __init__(self, store: SqlStore) -> None:
        self._store = store
        self._cache: dict[str, tuple[float, ApiKeyRecord | None]] = {}
        self._usage: Counter[str] = Counter()
        self._last_used: dict[str, datetime] = {}
        self._flusher: asyncio.Task[None] | None = None

    async def create(self, owner: str, owner_label: str, name: str) -> tuple[ApiKeyRecord, str]:
        """A new key for `owner`: (stored record, the full key to show once)."""
        name = name.strip()
        if not NAME_PATTERN.fullmatch(name):
            raise InvalidKeyName("Use 1-64 letters, digits, spaces or . , : ( ) / @ + -")
        active = [key for key in await self._store.list_keys(owner) if key.active]
        if len(active) >= MAX_KEYS_PER_OWNER:
            raise TooManyKeys
        for _ in range(3):  # a prefix collision is astronomically rare; retry anyway
            prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(8))
            secret = secrets.token_urlsafe(32)
            record = ApiKeyRecord(
                prefix=prefix,
                owner=owner,
                owner_label=owner_label,
                name=name,
                secret_hash=_hash(secret),
                created=utc_now(),
            )
            try:
                await self._store.create_key(record)
            except KeyExists:
                continue
            self._remember(prefix, record)
            return record, f"zk_{prefix}_{secret}"
        raise RuntimeError("could not allocate a key prefix")

    async def verify(self, token: str) -> KeyPrincipal | None:
        match = KEY_PATTERN.fullmatch(token)
        if match is None:
            return None
        prefix, secret = match.groups()
        record = await self._lookup(prefix)
        if record is None or not record.active:
            return None
        if not hmac.compare_digest(_hash(secret).encode(), record.secret_hash.encode()):
            return None
        return KeyPrincipal(prefix, record.owner, record.owner_label, record.name)

    def record_use(self, prefix: str) -> None:
        self._usage[prefix] += 1
        self._last_used[prefix] = utc_now()
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush_later())

    async def flush(self) -> None:
        """Write the counted usage: one update per key."""
        usage, self._usage = self._usage, Counter()
        last_used, self._last_used = self._last_used, {}
        for prefix, count in usage.items():
            try:
                await self._store.add_key_usage(prefix, count, last_used[prefix])
            except Exception:
                logger.exception("could not record key usage", extra={"fields": {"key": prefix}})

    async def revoke(self, prefix: str, owner: str | None = None) -> bool:
        """Revoke a key: the owner's own (owner given) or any key (an admin)."""
        changed = await self._store.revoke_key(prefix, utc_now(), owner)
        self._cache.pop(prefix, None)
        return changed

    async def list_keys(self, owner: str | None = None) -> list[ApiKeyRecord]:
        return await self._store.list_keys(owner)

    async def _lookup(self, prefix: str) -> ApiKeyRecord | None:
        cached = self._cache.get(prefix)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        record = await self._store.get_key(prefix)
        self._remember(prefix, record)  # misses too, so guessing cannot hammer the store
        return record

    def _remember(self, prefix: str, record: ApiKeyRecord | None) -> None:
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))  # oldest insertion first
        self._cache[prefix] = (time.monotonic() + CACHE_SECONDS, record)

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_SECONDS)
        await self.flush()
