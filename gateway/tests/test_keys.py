"""User API keys: storage, verification, revocation, usage, and the store implementations."""

import asyncio
import hashlib
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import QueuePool

from gateway.accounts import connect
from gateway.accounts import keys as keys_module
from gateway.accounts.authn import Authenticator
from gateway.accounts.keys import KEY_PATTERN, InvalidKeyName, KeyService, TooManyKeys
from gateway.accounts.migrate import migrate
from gateway.accounts.store import IdleCloser, SqlStore, connect_tls, create_engine
from gateway.auth import KeyRing
from tests.support import API_KEY, TEST_DATABASE_URL, close_store

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store() -> AsyncIterator[SqlStore]:
    store = SqlStore.from_url(TEST_DATABASE_URL)
    await store.ensure_schema()
    yield store
    await close_store(store)


@pytest.fixture
def service(store: SqlStore) -> KeyService:
    return KeyService(store)


async def test_a_new_key_is_shown_once_and_stored_only_as_a_hash(
    service: KeyService, store: SqlStore
) -> None:
    record, key = await service.create("user:alice", "alice@example.com", "laptop")
    match = KEY_PATTERN.fullmatch(key)
    assert match is not None
    prefix, secret = match.groups()
    assert record.prefix == prefix
    stored = await store.get_key(prefix)
    assert stored is not None
    assert stored.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in repr(stored)  # the secret itself is nowhere in storage


async def test_verify_accepts_the_key_and_nothing_else(service: KeyService) -> None:
    _, key = await service.create("user:alice", "alice@example.com", "laptop")
    principal = await service.verify(key)
    assert principal is not None
    assert principal.owner == "user:alice"
    assert await service.verify(key[:-1] + ("A" if key[-1] != "A" else "B")) is None
    assert await service.verify("zk_unknown1_" + "x" * 43) is None
    assert await service.verify("not a key") is None


async def test_revoked_keys_stop_working_at_once_in_this_process(service: KeyService) -> None:
    record, key = await service.create("user:alice", "alice@example.com", "laptop")
    assert await service.revoke(record.prefix, owner="user:mallory") is False  # not theirs
    assert await service.verify(key) is not None
    assert await service.revoke(record.prefix, owner="user:alice") is True
    assert await service.verify(key) is None
    assert await service.revoke(record.prefix) is False  # already revoked


async def test_revocation_elsewhere_applies_within_the_cache_window(
    service: KeyService, store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, key = await service.create("user:alice", "alice@example.com", "laptop")
    assert await service.verify(key) is not None
    await store.revoke_key(record.prefix, datetime.now(UTC))  # by another gateway task
    assert await service.verify(key) is not None  # still cached
    monkeypatch.setattr(keys_module, "CACHE_SECONDS", 0.0)
    service._cache.clear()
    assert await service.verify(key) is None


async def test_each_owner_has_at_most_five_active_keys(service: KeyService) -> None:
    created = [await service.create("user:alice", "a", f"key {n}") for n in range(5)]
    with pytest.raises(TooManyKeys):
        await service.create("user:alice", "a", "one too many")
    await service.revoke(created[0][0].prefix, owner="user:alice")
    await service.create("user:alice", "a", "after revoking one")  # room again
    await service.create("user:bob", "b", "other owners are unaffected")


@pytest.mark.parametrize("name", ["", " ", "x" * 65, "<script>", "line\nbreak"])
async def test_key_names_are_validated(service: KeyService, name: str) -> None:
    with pytest.raises(InvalidKeyName):
        await service.create("user:alice", "a", name)


async def test_usage_is_counted_and_flushed(service: KeyService, store: SqlStore) -> None:
    record, key = await service.create("user:alice", "alice@example.com", "laptop")
    authenticator = Authenticator(KeyRing({"smoke": API_KEY}), service)
    principals = [await authenticator.identify(key) for _ in range(3)]
    assert [p.label if p else None for p in principals] == [
        f"key:{record.prefix} (alice@example.com)"
    ] * 3
    await service.flush()
    stored = await store.get_key(record.prefix)
    assert stored is not None
    assert stored.use_count == 3
    assert stored.last_used is not None


async def test_operator_keys_still_work_and_are_not_counted(service: KeyService) -> None:
    authenticator = Authenticator(KeyRing({"smoke": API_KEY}), service)
    principal = await authenticator.identify(API_KEY)
    assert principal is not None
    assert principal.kind == "operator"
    assert principal.label == "operator:smoke"
    assert await authenticator.identify("wrong") is None


async def test_listing_is_per_owner_and_newest_first(service: KeyService) -> None:
    await service.create("user:alice", "a", "first")
    await service.create("user:bob", "b", "bob key")
    await service.create("user:alice", "a", "second")
    mine = await service.list_keys("user:alice")
    assert {key.name for key in mine} == {"first", "second"}
    assert len(await service.list_keys()) == 3


async def test_usage_keeps_the_latest_time(store: SqlStore, service: KeyService) -> None:
    record, _ = await service.create("user:alice", "a", "laptop")
    later = datetime(2026, 10, 17, 10, 1, tzinfo=UTC)
    await store.add_key_usage(record.prefix, 3, later)
    await store.add_key_usage(record.prefix, 2, datetime(2026, 10, 17, 10, 0, tzinfo=UTC))
    stored = await store.get_key(record.prefix)
    assert stored is not None
    assert (stored.use_count, stored.last_used) == (5, later)


async def test_migrations_are_idempotent(store: SqlStore) -> None:
    await migrate(store._engine)  # already at head: nothing to do, no error


def test_verify_full_trusts_only_the_rds_cas() -> None:
    url = "postgresql+asyncpg://u:p%40ss@db.cluster.example:5432/gateway?ssl=verify-full"
    plain, tls = connect_tls(url)
    assert plain == "postgresql+asyncpg://u:p%40ss@db.cluster.example:5432/gateway"
    context = tls["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    roots = context.get_ca_certs()
    assert len(roots) > 50
    assert all("Amazon" in str(cert["subject"]) for cert in roots)


@pytest.mark.parametrize("mode", ["require", "disable"])
def test_other_ssl_modes_pass_through(mode: str) -> None:
    url = f"postgresql+asyncpg://u:p@db:5432/gateway?ssl={mode}"
    assert connect_tls(url) == (url, {})


def pooled(engine: AsyncEngine) -> int:
    """Connections the pool holds open between queries."""
    pool = engine.sync_engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedin()


async def test_idle_connections_are_closed_so_aurora_can_pause(tmp_path: Path) -> None:
    """A pooled connection left open would keep Aurora Serverless from pausing."""
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'idle.db'}"  # a file: pooled like Postgres
        if TEST_DATABASE_URL.startswith("sqlite")
        else TEST_DATABASE_URL
    )
    engine = create_async_engine(url) if url.startswith("sqlite") else create_engine(url)
    closer = IdleCloser(engine, idle_seconds=0.2)
    try:
        for _ in range(2):  # and again after the pool was replaced
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            assert pooled(engine) == 1  # kept open for the next query
            await asyncio.sleep(0.1)
            assert pooled(engine) == 1  # not idle long enough yet
            await asyncio.sleep(0.3)
            assert pooled(engine) == 0  # closed
    finally:
        await closer.stop()
        await engine.dispose()


async def test_a_connection_in_use_is_not_closed_under_it(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}")
    closer = IdleCloser(engine, idle_seconds=0.1)
    try:
        async with engine.connect() as first:
            await first.execute(text("SELECT 1"))
        async with engine.connect() as busy:
            await asyncio.sleep(0.3)  # longer than the idle time, while in use
            assert (await busy.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await closer.stop()
        await engine.dispose()


class FlakyConnect:
    """Fails the engine's next `failures` connection attempts, as a resuming Aurora does."""

    def __init__(self, engine: AsyncEngine, error: Exception) -> None:
        self.attempts = 0
        self.failures = 0
        self._error = error
        event.listen(engine.sync_engine, "do_connect", self._connect)

    def _connect(self, *_: object) -> None:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise self._error


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("connect", {}, Exception("the database system is starting up")),
        ConnectionRefusedError("refused"),
        TimeoutError(),
    ],
)
async def test_opening_a_connection_is_retried_once_while_aurora_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(connect, "CONNECT_RETRY_SECONDS", 0.0)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resume.db'}")
    flaky = FlakyConnect(engine, error)
    store = SqlStore(engine)
    try:
        flaky.failures = 1
        await store.ensure_schema()  # the startup migration
        assert flaky.attempts == 2

        await engine.dispose()  # as IdleCloser does; Aurora then pauses
        flaky.failures = 1
        assert await store.get_key("abcdefgh") is None  # a query
        assert flaky.attempts == 4

        await engine.dispose()
        flaky.failures = 2  # still not up after the retry: the error reaches the caller
        with pytest.raises(type(error)):
            await store.get_key("abcdefgh")
        assert flaky.attempts == 6
    finally:
        await store.close()


async def test_a_failed_statement_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect, "CONNECT_RETRY_SECONDS", 0.0)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'statement.db'}")
    store = SqlStore(engine)
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_: statements.append(statement),
    )
    try:
        await store.ensure_schema()
        statements.clear()
        with pytest.raises(OperationalError):  # sqlite: no such table
            await store._execute(text("SELECT * FROM no_such_table"))
        assert statements == ["SELECT * FROM no_such_table"]
    finally:
        await store.close()
