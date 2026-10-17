"""Test doubles: settings, a scripted upstream HTTP server, and an in-memory ASGI client."""

import asyncio
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import h11
from sqlalchemy import delete

from gateway.accounts.schema import api_keys, invites, logins, usage_daily, users
from gateway.accounts.store import SqlStore
from gateway.config import Settings, Upstream, load_settings

API_KEY = "test-key-0123456789abcdef0123456789abcdef"
SESSION_SECRET = "session-secret-0123456789abcdef0123456789"

# The accounts store: SQLite in memory by default; set GATEWAY_TEST_DATABASE_URL
# (make test-postgres) to run the same tests against Postgres.
TEST_DATABASE_URL = os.environ.get("GATEWAY_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


async def close_store(store: SqlStore) -> None:
    """Empty a shared database for the next test (in-memory SQLite simply goes away)."""
    if not TEST_DATABASE_URL.startswith("sqlite"):
        async with store._engine.begin() as connection:
            for table in (logins, users, invites, api_keys, usage_daily):  # children first
                await connection.execute(delete(table))
    await store.close()


def make_settings(**env: str) -> Settings:
    base = {
        "GATEWAY_UPSTREAMS": "claude=http://127.0.0.1:1",
        "GATEWAY_API_KEYS": f"tester:{API_KEY}",
        "GATEWAY_SESSION_SECRET": SESSION_SECRET,
        "GATEWAY_COOKIE_SECURE": "false",
    }
    return load_settings({**base, **env})


class ServerConn:
    """The server side of one connection, driven step by step by a test handler."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.h11 = h11.Connection(h11.SERVER)

    async def _event(self) -> Any:
        while True:
            event = self.h11.next_event()
            if event is h11.NEED_DATA:
                self.h11.receive_data(await self.reader.read(65536))
                continue
            return event

    async def read_request(self) -> h11.Request:
        event = await self._event()
        assert isinstance(event, h11.Request), event
        return event

    async def read_chunk(self) -> bytes | None:
        """The next body chunk, or None at the end of the request body."""
        event = await self._event()
        if isinstance(event, h11.Data):
            return bytes(event.data)
        assert isinstance(event, h11.EndOfMessage), event
        return None

    async def read_body(self) -> bytes:
        body = b""
        while (chunk := await self.read_chunk()) is not None:
            body += chunk
        return body

    async def respond(self, status: int, headers: list[tuple[str, str]] | None = None) -> None:
        self._write(h11.Response(status_code=status, headers=headers or []))
        await self.writer.drain()

    async def write(self, data: bytes) -> None:
        self._write(h11.Data(data=data))
        await self.writer.drain()

    async def end(self) -> None:
        self._write(h11.EndOfMessage())
        await self.writer.drain()

    def close(self) -> None:
        """Close the connection (FIN), e.g. mid-response like a zip service aborting (§6.2)."""
        self.writer.close()

    async def wait_for_eof(self) -> None:
        while await self.reader.read(65536):
            pass

    def _write(self, event: h11.Response | h11.Data | h11.EndOfMessage) -> None:
        data = self.h11.send(event)
        if data:
            self.writer.write(data)


Handler = Callable[[ServerConn], Awaitable[None]]


class FakeUpstream:
    """A scripted HTTP/1.1 server on 127.0.0.1. `handler` runs once per connection."""

    def __init__(self, handler: Handler, name: str = "claude") -> None:
        self._handler = handler
        self._name = name
        self._server: asyncio.Server | None = None
        self.connections = 0
        self.handled = asyncio.Event()
        self.errors: list[BaseException] = []

    async def __aenter__(self) -> "FakeUpstream":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()

    @property
    def upstream(self) -> Upstream:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return Upstream(self._name, "127.0.0.1", port)

    @property
    def url(self) -> str:
        return f"http://{self.upstream.authority}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await self._handler(ServerConn(reader, writer))
        except Exception as exc:  # surfaced by tests that check `errors`
            self.errors.append(exc)
        finally:
            writer.close()
            self.handled.set()


class AsgiClient:
    """Drives an ASGI app the way uvicorn would, recording what it sends."""

    def __init__(
        self,
        method: str,
        path: str,
        headers: list[tuple[str, str]] | None = None,
        query: bytes = b"",
    ) -> None:
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers or []],
            "client": ("203.0.113.7", 50000),
            "server": ("gateway", 8000),
        }
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[MutableMapping[str, Any]] = []
        self.started = asyncio.Event()
        self.body_received = asyncio.Event()
        self.disconnect_on_send = False

    def feed(self, body: bytes, more: bool = False) -> None:
        self.incoming.put_nowait({"type": "http.request", "body": body, "more_body": more})

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "http.disconnect"})

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if self.disconnect_on_send:
            raise OSError("client went away")  # what uvicorn's ClientDisconnected is
        self.sent.append(message)
        if message["type"] == "http.response.start":
            self.started.set()
        elif message.get("body"):
            self.body_received.set()

    @property
    def status(self) -> int | None:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        return starts[0]["status"] if starts else None

    @property
    def headers(self) -> dict[str, str]:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        return {k.decode(): v.decode() for k, v in starts[0]["headers"]} if starts else {}

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")

    @property
    def completed(self) -> bool:
        """True only if the final body message (more_body=False) was sent."""
        return any(
            m["type"] == "http.response.body" and not m.get("more_body", False) for m in self.sent
        )
