import pytest

from gateway.ratelimit import RateLimiter
from tests.support import API_KEY
from tests.test_portal import client_for

pytestmark = pytest.mark.anyio


def test_limit_per_client_and_window() -> None:
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.retry_after("a", now=t) for t in (0, 1, 2)] == [None, None, None]
    assert limiter.retry_after("a", now=3) == 58  # until the first attempt leaves the window
    assert limiter.retry_after("b", now=3) is None  # another client is unaffected
    assert limiter.retry_after("a", now=60.5) is None  # the first attempt has expired


def test_memory_is_bounded() -> None:
    limiter = RateLimiter(limit=1, window_seconds=10, max_clients=2)
    limiter.retry_after("a", now=0)
    limiter.retry_after("b", now=0)
    limiter.retry_after("c", now=20)  # a and b are idle: forgotten, not kept forever
    assert set(limiter._hits) == {"c"}


async def test_key_sign_in_is_limited_per_client() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        wrong = {"api_key": "x" * 40, "next": "/docs"}
        statuses = [(await client.post("/docs/login", data=wrong)).status_code for _ in range(10)]
        assert statuses == [401] * 10
        limited = await client.post("/docs/login", data={"api_key": API_KEY, "next": "/docs"})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        # Behind the load balancer, the client is the last X-Forwarded-For entry.
        other = await client.post(
            "/docs/login",
            data={"api_key": API_KEY, "next": "/docs"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert other.status_code == 303
