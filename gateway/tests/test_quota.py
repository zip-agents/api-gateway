"""The per-user daily byte quota: counting, flushing, refusing, and who it applies to."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from gateway.accounts.authn import Authenticator
from gateway.accounts.keys import KeyService, utc_now
from gateway.accounts.quota import QuotaService, human_bytes, seconds_until_reset, utc_today
from gateway.accounts.service import Accounts
from gateway.accounts.store import SqlStore
from gateway.app import Gateway
from gateway.auth import KeyRing, Session, SessionSigner
from gateway.proxy import ServiceProxy
from tests.support import (
    API_KEY,
    SESSION_SECRET,
    TEST_DATABASE_URL,
    AsgiClient,
    FakeUpstream,
    ServerConn,
    close_store,
    make_settings,
)

pytestmark = pytest.mark.anyio

USER = "user:sub-ada"


@pytest.fixture
async def store() -> AsyncIterator[SqlStore]:
    store = SqlStore.from_url(TEST_DATABASE_URL)
    await store.ensure_schema()
    yield store
    await close_store(store)


# ---------------------------------------------------------------------- the service


async def test_usage_counts_until_the_quota_then_refuses(store: SqlStore) -> None:
    quota = QuotaService(store, daily_bytes=1000)
    assert await quota.refusal(USER) is None
    quota.record(USER, 600)
    assert await quota.used_today(USER) == 600  # counted before it is written
    assert await quota.refusal(USER) is None
    quota.record(USER, 400)
    refused = await quota.refusal(USER)
    assert refused is not None
    wait, message = refused
    assert 1 <= wait <= 86400
    assert "1000 B of your 1000 B" in message


async def test_flush_writes_one_row_per_user_and_day(store: SqlStore) -> None:
    quota = QuotaService(store, daily_bytes=10_000)
    quota.record(USER, 100)
    quota.record(USER, 250)
    quota.record("user:sub-bob", 5)
    await quota.flush()
    assert await store.daily_usage(USER, utc_today()) == 350
    assert await store.daily_usage_all(utc_today()) == {USER: 350, "user:sub-bob": 5}
    # Another gateway task (a fresh service) sees the stored total.
    assert await QuotaService(store, daily_bytes=10_000).used_today(USER) == 350
    quota.record(USER, 50)
    await quota.flush()  # adds to the row; does not replace it
    assert await store.daily_usage(USER, utc_today()) == 400


async def test_operators_and_a_zero_quota_are_never_limited(store: SqlStore) -> None:
    quota = QuotaService(store, daily_bytes=1)
    quota.record("op:smoke", 10**9)
    assert await quota.refusal("op:smoke") is None
    unlimited = QuotaService(store, daily_bytes=0)
    unlimited.record(USER, 10**12)
    assert await unlimited.refusal(USER) is None


async def test_a_database_failure_lets_requests_through(
    store: SqlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    quota = QuotaService(store, daily_bytes=1)
    quota.record(USER, 10)  # over quota, if the total could be read

    async def unavailable(*_: object) -> int:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(store, "daily_usage", unavailable)
    assert await quota.refusal(USER) is None


async def test_old_usage_is_pruned(store: SqlStore) -> None:
    await store.add_daily_usage(USER, datetime(2020, 1, 1, tzinfo=UTC).date(), 99, 1)
    await QuotaService(store, daily_bytes=1).flush()
    assert await store.daily_usage(USER, datetime(2020, 1, 1, tzinfo=UTC).date()) == 0


def test_reset_and_formatting() -> None:
    assert seconds_until_reset(datetime(2026, 10, 17, 23, 59, 30, tzinfo=UTC)) == 30
    assert seconds_until_reset(datetime(2026, 10, 17, 0, 0, tzinfo=UTC)) == 86400
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(10 * 1024**3) == "10 GiB"


# ---------------------------------------------------------------------- the API


async def echo(conn: ServerConn) -> None:
    """A stand-in zip service: returns as many bytes as it was sent."""
    await conn.read_request()
    body = await conn.read_body()
    await conn.respond(200, [("Content-Length", str(len(body))), ("Connection", "close")])
    await conn.write(body)
    await conn.end()


async def zip_call(proxy: ServiceProxy, fake: FakeUpstream, key: str, body: bytes) -> AsgiClient:
    client = AsgiClient(
        "POST",
        "/claude/api/v1/zip",
        [("Authorization", f"Bearer {key}"), ("Content-Length", str(len(body)))],
    )
    client.feed(body)
    await asyncio.wait_for(
        proxy(client.scope, client.receive, client.send, fake.upstream, "rid"), timeout=10
    )
    return client


async def test_api_requests_are_refused_once_the_quota_is_used(store: SqlStore) -> None:
    keys = KeyService(store)
    _, user_key = await keys.create(USER, "ada@example.com", "laptop")
    quota = QuotaService(store, daily_bytes=1500)
    async with FakeUpstream(echo) as fake:
        settings = make_settings(GATEWAY_UPSTREAMS=f"claude=http://{fake.upstream.authority}")
        proxy = ServiceProxy(settings, Authenticator(KeyRing(settings.api_keys), keys), quota)

        first = await zip_call(proxy, fake, user_key, b"x" * 500)  # 500 in + 500 out
        assert first.status == 200
        assert await quota.used_today(USER) == 1000
        second = await zip_call(proxy, fake, user_key, b"x" * 500)  # allowed: under at start
        assert second.status == 200

        refused = await zip_call(proxy, fake, user_key, b"x" * 10)
        assert refused.status == 429
        assert int(refused.headers["retry-after"]) >= 1
        problem = json.loads(refused.body)
        assert problem["type"] == "urn:zip-gateway:problem:quota-exceeded"
        assert "resets at 00:00 UTC" in problem["detail"]
        assert fake.connections == 2  # the refused request never reached the service

        operator = await zip_call(proxy, fake, API_KEY, b"x" * 10)  # operators are exempt
        assert operator.status == 200


# ---------------------------------------------------------------------- the browser page


async def test_browser_jobs_are_refused_once_the_quota_is_used(store: SqlStore) -> None:
    settings = make_settings(GATEWAY_USER_DAILY_QUOTA_MIB="1")
    accounts = Accounts.build(settings, store)
    app = Gateway(settings, accounts=accounts)
    await store.record_login(USER[5:], "ada@example.com", None, utc_now(), None, None)
    signer = SessionSigner(SESSION_SECRET, 3600, KeyRing(settings.api_keys))
    session = signer.issue(Session(USER, "ada@example.com"))
    accounts.quota.record(USER, 2 * 1024 * 1024)

    client = AsgiClient(
        "POST",
        "/docs/zip/jobs",
        [
            ("Host", "gw"),
            ("Origin", "http://gw"),
            ("Cookie", f"gw_docs_session={session}"),
            ("X-CSRF-Token", signer.csrf_token(session)),
            ("Content-Type", "application/json"),
        ],
    )
    client.feed(json.dumps({"service": "claude"}).encode())
    await asyncio.wait_for(app(client.scope, client.receive, client.send), timeout=10)
    assert client.status == 429
    assert json.loads(client.body)["type"] == "urn:zip-gateway:problem:quota-exceeded"


def test_the_quota_is_configurable() -> None:
    assert make_settings().user_daily_quota_bytes == 10 * 1024**3
    assert make_settings(GATEWAY_USER_DAILY_QUOTA_MIB="0").user_daily_quota_bytes == 0
    assert make_settings(GATEWAY_USER_DAILY_QUOTA_MIB="5").user_daily_quota_bytes == 5 * 1024**2
