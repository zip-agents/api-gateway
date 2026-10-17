"""A minimal HTTP/1.1 client connection to one zip service, built on h11.

Why not httpx or aiohttp: the zip services stream full-duplex (they send archive bytes
while the upload is still arriving, see contract §4.1). httpx sends the whole request body
before it reads any of the response, which would turn every client into a half-duplex
client and cap uploads at what fits in the service's output buffer. With h11 the gateway
owns both directions of the socket and pumps them independently (see proxy.py).

One connection per proxied request; nothing is pooled. Upstreams are on the private
network, so the connect cost is small next to a streamed upload.
"""

import asyncio
from typing import Self

import h11

from gateway.config import Upstream

READ_SIZE = 64 * 1024


class UpstreamConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._h11 = h11.Connection(h11.CLIENT)

    @classmethod
    async def open(cls, upstream: Upstream, timeout: float) -> Self:
        """Connect, raising OSError or TimeoutError if the service cannot be reached."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(upstream.host, upstream.port), timeout
        )
        return cls(reader, writer)

    async def send(self, event: h11.Request | h11.Data | h11.EndOfMessage) -> None:
        """Send one event; waits while the service is not reading (backpressure)."""
        data = self._h11.send(event)
        if data:
            self._writer.write(data)
            await self._writer.drain()

    async def next_event(self) -> h11.Event:
        """Read the next response event. Raises h11.RemoteProtocolError on invalid input."""
        while True:
            event = self._h11.next_event()
            if event is h11.NEED_DATA:
                self._h11.receive_data(await self._reader.read(READ_SIZE))
            elif event is h11.PAUSED:
                # Only reachable by reading past the end of the response, which callers
                # never do; treat it as a protocol error rather than spin.
                raise h11.RemoteProtocolError("read past the end of the response")
            else:
                assert isinstance(event, h11.Event)
                return event

    def close(self) -> None:
        """Close at once, without flushing: the service sees a disconnect immediately."""
        self._writer.transport.abort()


async def fetch(
    upstream: Upstream, method: str, path: str, timeout: float, max_bytes: int = READ_SIZE
) -> tuple[int, bytes]:
    """Small whole-body GET for the docs portal. Raises on any failure or oversize body."""

    async def exchange() -> tuple[int, bytes]:
        conn = await UpstreamConnection.open(upstream, timeout)
        try:
            await conn.send(
                h11.Request(
                    method=method,
                    target=path,
                    headers=[("Host", upstream.authority), ("Connection", "close")],
                )
            )
            await conn.send(h11.EndOfMessage())
            status, body = 0, bytearray()
            while True:
                event = await conn.next_event()
                if isinstance(event, h11.Response):
                    status = event.status_code
                elif isinstance(event, h11.Data):
                    body += event.data
                    if len(body) > max_bytes:
                        raise ValueError("response too large")
                elif isinstance(event, h11.EndOfMessage):
                    return status, bytes(body)
                elif isinstance(event, h11.ConnectionClosed):
                    raise ConnectionError("closed before the response completed")
        finally:
            conn.close()

    return await asyncio.wait_for(exchange(), timeout)
