"""Full-duplex streaming reverse proxy from `/<service>/<path>` to `<upstream>/<path>`.

The zip services' streaming guarantees must survive the extra hop:

* Nothing is buffered beyond one chunk in each direction (plus uvicorn's and the
  kernel's socket buffers), so memory per request stays bounded and independent of
  upload size.
* The upload and the download are pumped by separate tasks, so a full-duplex client gets
  archive bytes while it is still uploading. Backpressure passes through: if the client
  stops reading, `send()` blocks, the gateway stops reading from the service, and the
  service's own output buffer and idle timeout take over (contract §4.1).
* A service that aborts mid-stream (contract §6.2) closes its connection without the
  terminating chunk. The gateway then returns without completing its own response, so
  uvicorn closes the client connection without a terminating chunk too, and the client
  sees the same truncated archive it would have seen talking to the service directly.
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

import h11
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from gateway.accounts.authn import Authenticator
from gateway.accounts.quota import QuotaService
from gateway.auth import bearer_token
from gateway.config import Settings, Upstream
from gateway.problems import problem_response
from gateway.upstream import UpstreamConnection

logger = logging.getLogger("gateway.proxy")

# RFC 9110 §7.6.1 hop-by-hop headers, plus headers the gateway replaces or must not leak:
# `authorization` (the gateway's API key), `cookie` (docs session), `host` (rewritten),
# `expect` (uvicorn already answered `100 Continue` to the client).
_DROP_REQUEST = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"host",
        b"authorization",
        b"cookie",
        b"expect",
        b"x-request-id",
        b"x-forwarded-for",
        b"x-forwarded-proto",
        b"x-forwarded-host",
        b"x-forwarded-prefix",
        b"x-csrf-token",  # the portal's, for browser upload jobs
    }
)
# uvicorn frames the response itself (chunked unless Content-Length is passed through).
# A service has no business setting cookies on the gateway's origin.
_DROP_RESPONSE = frozenset(
    {
        b"set-cookie",
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)


class ClientDisconnected(Exception):
    pass


class GatewayFailure(Exception):
    """A failure the gateway reports as `code` before streaming, or aborts on after."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@dataclass
class Stats:
    status: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    outcome: str = "completed"
    code: str | None = None


def upstream_request_headers(
    scope: Scope, upstream: Upstream, request_id: str, prefix: str
) -> list[tuple[bytes, bytes]]:
    """Headers to send upstream: client headers minus hop-by-hop, plus forwarding info."""
    request_headers = Headers(scope=scope)
    # Headers named in `Connection` are hop-by-hop for this request too (RFC 9110 §7.6.1).
    listed = {
        token.strip().lower().encode("latin-1")
        for value in request_headers.getlist("connection")
        for token in value.split(",")
        if token.strip()
    }
    chunked = "transfer-encoding" in request_headers
    headers: list[tuple[bytes, bytes]] = [(b"host", upstream.authority.encode("ascii"))]
    for name, value in scope["headers"]:
        name = name.lower()
        if name in _DROP_REQUEST or name in listed:
            continue
        if chunked and name == b"content-length":
            continue  # RFC 9112 §6.3: Transfer-Encoding wins; never forward both.
        headers.append((name, value))
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))

    client = scope.get("client")
    forwarded_for = request_headers.get("x-forwarded-for")
    if client:
        forwarded_for = f"{forwarded_for}, {client[0]}" if forwarded_for else client[0]
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    proto = request_headers.get("x-forwarded-proto") or scope.get("scheme", "http")
    headers.append((b"x-forwarded-proto", proto.encode("latin-1")))
    if "host" in request_headers:
        headers.append((b"x-forwarded-host", request_headers["host"].encode("latin-1")))
    headers.append((b"x-forwarded-prefix", prefix.encode("ascii")))
    headers.append((b"x-request-id", request_id.encode("ascii")))
    return headers


def downstream_response_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    return [(name.lower(), value) for name, value in headers if name.lower() not in _DROP_RESPONSE]


def upstream_target(scope: Scope, prefix: str) -> bytes:
    """`/claude/api/v1/zip?x=1` -> `/api/v1/zip?x=1`, preserving the raw encoding."""
    raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
    rest = raw_path[len(prefix) :] or b"/"
    query: bytes = scope.get("query_string", b"")
    return rest + b"?" + query if query else rest


def log_request(
    method: str,
    path: str,
    service: str,
    api_key: str | None,
    stats: Stats,
    started: float,
    **extra: object,
) -> None:
    """The one completion line per proxied request (or browser job)."""
    level = logging.WARNING if stats.outcome != "completed" else logging.INFO
    logger.log(
        level,
        "request finished",
        extra={
            "fields": {
                "method": method,
                "path": path,
                "service": service,
                "api_key": api_key,
                "status": stats.status,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "bytes_in": stats.bytes_in,
                "bytes_out": stats.bytes_out,
                "outcome": stats.outcome,
                "code": stats.code,
                **extra,
            }
        },
    )


class ServiceProxy:
    def __init__(
        self,
        settings: Settings,
        authenticator: Authenticator,
        quota: QuotaService | None = None,
    ) -> None:
        self._settings = settings
        self._authenticator = authenticator
        self._quota = quota

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, upstream: Upstream, request_id: str
    ) -> None:
        started = time.monotonic()
        stats = Stats()
        key_name: str | None = None
        try:
            key_name = await self._handle(scope, receive, send, upstream, request_id, stats)
        except Exception:
            stats.outcome, stats.code = "aborted", "internal-error"
            logger.exception("proxy failed")
            raise
        finally:
            log_request(scope["method"], scope["path"], upstream.name, key_name, stats, started)

    async def _handle(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        upstream: Upstream,
        request_id: str,
        stats: Stats,
    ) -> str | None:
        headers = Headers(scope=scope)
        token = bearer_token(headers.get("authorization"))
        principal = await self._authenticator.identify(token) if token else None
        if principal is None:
            await self._reject(
                scope,
                receive,
                send,
                stats,
                "unauthorized",
                request_id,
                {"WWW-Authenticate": 'Bearer realm="zip-gateway"'},
            )
            return None

        if self._quota is not None:
            refused = await self._quota.refusal(principal.owner)
            if refused is not None:
                wait, detail = refused
                await self._reject(
                    scope,
                    receive,
                    send,
                    stats,
                    "quota-exceeded",
                    request_id,
                    {"Retry-After": str(wait)},
                    detail=detail,
                )
                return principal.label

        target = upstream_target(scope, f"/{upstream.name}")
        try:
            await self.forward(scope, receive, send, upstream, request_id, target, stats)
        finally:
            if self._quota is not None:
                self._quota.record(principal.owner, stats.bytes_in + stats.bytes_out)
        return principal.label

    async def forward(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        upstream: Upstream,
        request_id: str,
        target: bytes,
        stats: Stats,
    ) -> None:
        """Stream one already-authenticated request to `target` on `upstream`.

        Everything the client should see, including problem responses, goes to `send`;
        failures are recorded in `stats`. Used by the API routes and by browser upload
        jobs (browser_jobs.py), whose `send` writes to the job's download side.
        """
        prefix = f"/{upstream.name}"
        request = h11.Request(
            method=scope["method"].encode("ascii"),
            target=target,
            headers=upstream_request_headers(scope, upstream, request_id, prefix),
        )
        try:
            conn = await UpstreamConnection.open(upstream, self._settings.connect_timeout_seconds)
        except (OSError, TimeoutError) as exc:
            logger.warning(
                "upstream unreachable",
                extra={"fields": {"service": upstream.name, "error": repr(exc)}},
            )
            await self._reject(scope, receive, send, stats, "upstream-unavailable", request_id)
            return

        exchange = Exchange(conn, receive, send, self._settings.idle_timeout_seconds, stats)
        try:
            await exchange.run(request)
        except ClientDisconnected:
            stats.outcome, stats.code = "client_disconnected", "client-disconnected"
        except GatewayFailure as failure:
            logger.warning(
                "upstream failed",
                extra={"fields": {"service": upstream.name, "reason": str(failure)}},
            )
            if exchange.response_started:
                # Mid-stream: return without the final body message. uvicorn closes the
                # connection without the terminating chunk; the client's archive is
                # visibly truncated, exactly as contract §6.2 requires of the service.
                stats.outcome, stats.code = "aborted", failure.code
            else:
                await self._reject(
                    scope, receive, send, stats, failure.code, request_id, {"Connection": "close"}
                )

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        stats: Stats,
        code: str,
        request_id: str,
        headers: dict[str, str] | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        response = problem_response(code, request_id, detail=detail, headers=headers)
        stats.outcome, stats.code, stats.status = "rejected", code, response.status_code
        try:
            await response(scope, receive, send)
        except OSError:
            stats.outcome = "client_disconnected"


class Exchange:
    """One proxied request: an upload pump, a download pump and an idle watchdog.

    `run` returns once the response has been passed on completely, and raises
    `ClientDisconnected` or `GatewayFailure` otherwise. Whatever happens, all three tasks
    are cancelled and the upstream connection is closed before it returns.
    """

    def __init__(
        self,
        conn: UpstreamConnection,
        receive: Receive,
        send: Send,
        idle_timeout: float,
        stats: Stats,
    ) -> None:
        self._conn = conn
        self._receive = receive
        self._send = send
        self._idle_timeout = idle_timeout
        self._stats = stats
        self._last_progress = time.monotonic()
        self.response_started = False

    async def run(self, request: h11.Request) -> None:
        upload = asyncio.create_task(self._upload(request))
        download = asyncio.create_task(self._download())
        watchdog = asyncio.create_task(self._watchdog())
        tasks = (upload, download, watchdog)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Only the download can finish without raising. Check it first: once the
            # response is complete, uvicorn's receive() reports a disconnect, so the
            # upload task may finish in the same instant with ClientDisconnected.
            if download in done:
                download.result()
            else:
                next(iter(done)).result()
                raise AssertionError("upload and watchdog tasks only finish by raising")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._conn.close()

    def _progress(self) -> None:
        self._last_progress = time.monotonic()

    async def _upload(self, request: h11.Request) -> None:
        try:
            await self._conn.send(request)
        except OSError as exc:
            raise GatewayFailure("upstream-error", f"sending request headers: {exc!r}") from exc
        upstream_reading = True
        more_body = True
        while more_body:
            message = await self._receive()
            if message["type"] == "http.disconnect":
                raise ClientDisconnected
            body: bytes = message.get("body", b"")
            more_body = message.get("more_body", False)
            if body:
                self._stats.bytes_in += len(body)
                self._progress()
            if not upstream_reading:
                continue  # Drain the client so it can read the response; see below.
            try:
                if body:
                    await self._conn.send(h11.Data(data=body))
                if not more_body:
                    await self._conn.send(h11.EndOfMessage())
            except (OSError, h11.LocalProtocolError):
                # The service stopped reading, usually because it has already answered
                # (an early 4xx). Keep reading the client so a half-duplex client can
                # finish sending and see that answer; _download decides the outcome.
                upstream_reading = False
            else:
                self._progress()
        # Request complete. Keep listening, so that a client that goes away while the
        # response is streaming stops the download promptly (contract §6.2).
        while True:
            if (await self._receive())["type"] == "http.disconnect":
                raise ClientDisconnected

    async def _download(self) -> None:
        while True:
            event = await self._next_event()
            if isinstance(event, h11.InformationalResponse):
                continue
            if isinstance(event, h11.Response):
                self._stats.status = event.status_code
                await self._deliver(
                    {
                        "type": "http.response.start",
                        "status": event.status_code,
                        "headers": downstream_response_headers(event.headers),
                    }
                )
                self.response_started = True
            elif isinstance(event, h11.Data):
                self._progress()
                await self._deliver(
                    {"type": "http.response.body", "body": bytes(event.data), "more_body": True}
                )
                self._stats.bytes_out += len(event.data)
                self._progress()
            elif isinstance(event, h11.EndOfMessage):
                await self._deliver({"type": "http.response.body", "body": b"", "more_body": False})
                return
            else:  # h11.ConnectionClosed
                raise GatewayFailure("upstream-error", "upstream closed the connection early")

    async def _next_event(self) -> h11.Event:
        try:
            return await self._conn.next_event()
        except h11.RemoteProtocolError as exc:
            # Includes the service closing mid-body: the §6.2 abort, seen from this side.
            raise GatewayFailure("upstream-error", f"invalid or truncated response: {exc}") from exc
        except OSError as exc:
            raise GatewayFailure("upstream-error", f"reading response: {exc!r}") from exc

    async def _deliver(self, message: Message) -> None:
        try:
            await self._send(message)
        except OSError as exc:  # uvicorn raises ClientDisconnected, an OSError
            raise ClientDisconnected from exc

    async def _watchdog(self) -> None:
        while True:
            remaining = self._last_progress + self._idle_timeout - time.monotonic()
            if remaining <= 0:
                raise GatewayFailure(
                    "upstream-timeout",
                    f"no progress in either direction for {self._idle_timeout:g}s",
                )
            await asyncio.sleep(remaining)
