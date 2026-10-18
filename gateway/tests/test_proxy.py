"""The streaming proxy against a scripted upstream over real local sockets."""

import asyncio
import json
import socket
import time

import pytest

from gateway.accounts.authn import Authenticator
from gateway.auth import KeyRing
from gateway.config import Settings, Upstream
from gateway.proxy import ServiceProxy, upstream_request_headers, upstream_target
from tests.support import API_KEY, AsgiClient, FakeUpstream, ServerConn, make_settings

pytestmark = pytest.mark.anyio

AUTH = ("Authorization", f"Bearer {API_KEY}")


def proxy_for(upstream: Upstream, **env: str) -> ServiceProxy:
    settings = make_settings(GATEWAY_UPSTREAMS=f"claude=http://{upstream.authority}", **env)
    return ServiceProxy(settings, Authenticator(KeyRing(settings.api_keys)))


async def run(proxy: ServiceProxy, client: AsgiClient, upstream: Upstream) -> None:
    await asyncio.wait_for(
        proxy(client.scope, client.receive, client.send, upstream, "rid-1"), timeout=10
    )


def problem(client: AsgiClient) -> dict[str, object]:
    assert client.headers["content-type"] == "application/problem+json"
    body: dict[str, object] = json.loads(client.body)
    return body


async def test_forwards_request_and_streams_response() -> None:
    seen: dict[str, object] = {}

    async def handler(conn: ServerConn) -> None:
        request = await conn.read_request()
        seen["target"] = request.target
        seen["headers"] = {k.decode(): v.decode() for k, v in request.headers}
        seen["body"] = await conn.read_body()
        await conn.respond(200, [("Content-Type", "application/zip"), ("Connection", "close")])
        await conn.write(b"PK\x03\x04")
        await conn.write(b"rest")
        await conn.end()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient(
            "POST",
            "/claude/api/v1/zip",
            [
                AUTH,
                ("Host", "gw.example"),
                ("Content-Length", "5"),
                ("Cookie", "gw_docs_session=x"),
                ("X-Forwarded-For", "198.51.100.1"),
                ("X-Request-ID", "spoofed"),
            ],
            query=b"x=1",
        )
        client.feed(b"hello")
        await run(proxy_for(fake.upstream), client, fake.upstream)
        authority = fake.upstream.authority  # read while the server is still listening

    assert seen["target"] == b"/api/v1/zip?x=1"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["host"] == authority
    assert headers["content-length"] == "5"
    assert headers["x-request-id"] == "rid-1"
    assert headers["x-forwarded-for"] == "198.51.100.1, 203.0.113.7"
    assert headers["x-forwarded-host"] == "gw.example"
    assert headers["x-forwarded-prefix"] == "/claude"
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert seen["body"] == b"hello"

    assert client.status == 200
    assert client.headers["content-type"] == "application/zip"
    assert "connection" not in client.headers
    assert client.body == b"PK\x03\x04rest"
    assert client.completed


async def test_chunked_upload_is_forwarded_chunked() -> None:
    seen: dict[str, object] = {}

    async def handler(conn: ServerConn) -> None:
        request = await conn.read_request()
        seen["headers"] = {k.decode(): v.decode() for k, v in request.headers}
        seen["body"] = await conn.read_body()
        await conn.respond(200, [("Content-Length", "0")])
        await conn.end()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient(
            "POST",
            "/claude/api/v1/zip",
            [AUTH, ("Transfer-Encoding", "chunked"), ("Content-Length", "999")],
        )
        client.feed(b"one-", more=True)
        client.feed(b"two")
        await run(proxy_for(fake.upstream), client, fake.upstream)

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["transfer-encoding"] == "chunked"
    assert "content-length" not in headers
    assert seen["body"] == b"one-two"
    assert client.status == 200


async def test_full_duplex_response_flows_before_upload_finishes() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        assert await conn.read_chunk() == b"part1"
        await conn.respond(200)
        await conn.write(b"early")
        assert await conn.read_body() == b"part2"
        await conn.write(b"late")
        await conn.end()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Transfer-Encoding", "chunked")])
        client.feed(b"part1", more=True)
        task = asyncio.create_task(run(proxy_for(fake.upstream), client, fake.upstream))
        # The second part is only sent once archive bytes have arrived: a proxy that
        # buffered the request (or read the response only after it) would deadlock here.
        await asyncio.wait_for(client.body_received.wait(), timeout=5)
        assert client.body == b"early"
        client.feed(b"part2")
        await task

    assert client.body == b"earlylate"
    assert client.completed
    assert not fake.errors


async def test_upstream_abort_mid_stream_truncates_response() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(200, [("Content-Type", "application/zip")])
        await conn.write(b"partial")
        await asyncio.sleep(0.1)
        conn.close()  # no terminating chunk: the service's §6.2 abort

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "1")])
        client.feed(b"x")
        await run(proxy_for(fake.upstream), client, fake.upstream)

    assert client.status == 200
    assert client.body == b"partial"
    # The final body message is never sent, so uvicorn closes the client connection
    # without a terminating chunk.
    assert not client.completed


async def test_unreachable_upstream_is_502() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    upstream = Upstream("claude", "127.0.0.1", port)  # nothing listens here now
    client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "0")])
    client.feed(b"")
    await run(proxy_for(upstream), client, upstream)

    assert client.status == 502
    body = problem(client)
    assert body["code"] == "upstream-unavailable"
    assert body["type"] == "urn:zip-gateway:problem:upstream-unavailable"
    assert body["request_id"] == "rid-1"


async def test_upstream_closing_before_response_is_502() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        conn.close()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "1")])
        client.feed(b"x")
        await run(proxy_for(fake.upstream), client, fake.upstream)

    assert client.status == 502
    assert problem(client)["code"] == "upstream-error"
    assert client.completed


async def test_early_upstream_error_reaches_a_client_still_uploading() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        body = b'{"code":"unsupported-media-type"}'
        await conn.respond(
            415,
            [("Content-Type", "application/problem+json"), ("Content-Length", str(len(body)))],
        )
        await conn.write(body)
        await conn.end()
        await asyncio.sleep(0.5)  # stop reading, as a service does after rejecting

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Transfer-Encoding", "chunked")])
        client.feed(b"still uploading", more=True)
        await run(proxy_for(fake.upstream), client, fake.upstream)

    assert client.status == 415
    assert client.body == b'{"code":"unsupported-media-type"}'
    assert client.completed


async def test_idle_timeout_before_response_is_504() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.wait_for_eof()  # never answers

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "1")])
        client.feed(b"x")
        started = time.monotonic()
        await run(proxy_for(fake.upstream, GATEWAY_IDLE_TIMEOUT_SECONDS="1"), client, fake.upstream)

    assert 0.9 < time.monotonic() - started < 5
    assert client.status == 504
    assert problem(client)["code"] == "upstream-timeout"


async def test_idle_timeout_mid_stream_aborts() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(200)
        await conn.write(b"some")
        await conn.wait_for_eof()  # stalls without closing

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "1")])
        client.feed(b"x")
        await run(proxy_for(fake.upstream, GATEWAY_IDLE_TIMEOUT_SECONDS="1"), client, fake.upstream)

    assert client.status == 200
    assert client.body == b"some"
    assert not client.completed


async def test_client_disconnect_during_upload_closes_upstream() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_chunk()
        await conn.wait_for_eof()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Transfer-Encoding", "chunked")])
        client.feed(b"part", more=True)
        client.disconnect()
        await run(proxy_for(fake.upstream), client, fake.upstream)
        # The upstream sees the connection close promptly, so it can release resources.
        await asyncio.wait_for(fake.handled.wait(), timeout=5)

    assert client.status is None


async def test_client_disconnect_during_download_closes_upstream() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(200)
        while True:  # streams until the gateway hangs up
            await conn.write(b"x" * 65536)
            await asyncio.sleep(0.01)

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "1")])
        client.feed(b"x")
        task = asyncio.create_task(run(proxy_for(fake.upstream), client, fake.upstream))
        await asyncio.wait_for(client.body_received.wait(), timeout=5)
        client.disconnect()
        await task
        await asyncio.wait_for(fake.handled.wait(), timeout=5)

    assert not client.completed


async def test_missing_key_is_401_and_never_reaches_upstream() -> None:
    async def handler(conn: ServerConn) -> None:
        raise AssertionError("upstream must not be contacted")

    async with FakeUpstream(handler) as fake:
        for headers in ([], [("Authorization", "Bearer wrong-key")], [("Authorization", API_KEY)]):
            client = AsgiClient("POST", "/claude/api/v1/zip", headers)
            client.feed(b"")
            await run(proxy_for(fake.upstream), client, fake.upstream)
            assert client.status == 401
            assert client.headers["www-authenticate"].startswith("Bearer")
            assert problem(client)["code"] == "unauthorized"

    assert fake.connections == 0


def test_upstream_target_strips_prefix_and_keeps_raw_encoding() -> None:
    scope = {"path": "/claude/a b", "raw_path": b"/claude/a%20b", "query_string": b"q=1"}
    assert upstream_target(scope, "/claude") == b"/a%20b?q=1"
    assert upstream_target({"path": "/claude", "raw_path": b"/claude"}, "/claude") == b"/"


def test_headers_named_in_connection_are_not_forwarded() -> None:
    settings: Settings = make_settings()
    scope = AsgiClient(
        "GET", "/claude/x", [("Connection", "keep-alive, X-Secret"), ("X-Secret", "1")]
    ).scope
    headers = upstream_request_headers(scope, settings.upstreams["claude"], "r", "/claude")
    names = {name for name, _ in headers}
    assert b"x-secret" not in names
    assert b"connection" not in names


async def test_a_service_cannot_set_cookies_on_the_gateway_origin() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(
            200,
            [
                ("Content-Type", "application/zip"),
                ("Set-Cookie", "gw_docs_session=forged; Path=/"),
                ("Content-Disposition", 'attachment; filename="a.zip"'),
            ],
        )
        await conn.end()

    async with FakeUpstream(handler) as fake:
        client = AsgiClient("POST", "/claude/api/v1/zip", [AUTH, ("Content-Length", "0")])
        await run(proxy_for(fake.upstream), client, fake.upstream)

    assert client.status == 200
    assert "set-cookie" not in client.headers
    # The API path otherwise passes the service's headers through untouched.
    assert client.headers["content-disposition"] == 'attachment; filename="a.zip"'
