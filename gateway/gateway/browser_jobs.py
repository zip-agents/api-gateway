"""Browser upload jobs: full-duplex streaming for clients that can only do half.

Browsers send the whole request body before they read any of the response, so a single
upload request is limited by the service's output buffer (contract §4.1). A job splits
the work into two requests that run at the same time, and joins them here into one
streaming exchange with the service:

    POST /zip/jobs                   {"service": "claude"} -> {"id", "upload", "download"}
    GET  /zip/jobs/<id>/archive      the archive: a normal browser download, streamed
                                          to disk under the service's Content-Disposition
    POST /zip/jobs/<id>/files        the multipart upload; answered after the exchange
                                          with a small JSON outcome

The upload request's body feeds the service (ServiceProxy.forward, the same path as the
API), and the service's response is passed through a one-message pipe to the download
request. Backpressure therefore holds end to end. Jobs are bound to the signed-in user,
expire if both halves do not arrive within the attach timeout, and are capped in number.
"""

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from gateway.accounts.quota import QuotaService
from gateway.accounts.users import UserGate
from gateway.auth import SESSION_COOKIE, SessionSigner
from gateway.config import Settings
from gateway.origin import same_origin
from gateway.problems import problem_response
from gateway.proxy import ServiceProxy, Stats, log_request

logger = logging.getLogger("gateway.browser_jobs")

ATTACH_TIMEOUT_SECONDS = 30.0
MAX_JOBS_PER_USER = 4
MAX_PROBLEM_BYTES = 64 * 1024
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class DownloadGone(OSError):
    """The download half went away. An OSError, like any other vanished client, so the
    proxy's existing handling (ClientDisconnected, then cleanup) applies unchanged."""


class Pipe:
    """Hands ASGI response messages from the exchange to the download request.

    Holds at most one message: a slow download stops the exchange reading from the
    service, which stops the service reading the upload. Memory stays bounded.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=1)
        self._gone = asyncio.Event()

    @property
    def gone(self) -> bool:
        return self._gone.is_set()

    async def send(self, message: Message) -> None:
        """The exchange's `send`. Raises DownloadGone once the download has gone."""
        if not await self._put(message):
            raise DownloadGone("download closed")

    async def finish(self) -> None:
        """No more messages: if the response is incomplete, the download is cut."""
        await self._put(None)

    async def _put(self, message: Message | None) -> bool:
        """Queue `message` unless the download goes first; True if it was queued."""
        if self._gone.is_set():
            return False
        put = asyncio.ensure_future(self._queue.put(message))
        gone = asyncio.ensure_future(self._gone.wait())
        await asyncio.wait({put, gone}, return_when=asyncio.FIRST_COMPLETED)
        gone.cancel()
        if put.done():
            return True
        put.cancel()
        return False

    async def receive(self) -> Message | None:
        """The next message for the download, or None when the exchange has ended."""
        return await self._queue.get()

    def close(self) -> None:
        self._gone.set()


@dataclass
class Job:
    id: str
    service: str
    owner: str
    created: float = field(default_factory=time.monotonic)
    pipe: Pipe = field(default_factory=Pipe)
    download_attached: asyncio.Event = field(default_factory=asyncio.Event)
    upload_started: asyncio.Event = field(default_factory=asyncio.Event)
    exchange: "asyncio.Task[None] | None" = None
    cancelled: bool = False

    def cancel(self) -> None:
        """The download went away: stop the exchange and close the service connection."""
        self.cancelled = True
        self.pipe.close()
        if self.exchange is not None:
            self.exchange.cancel()


class TooManyJobs(Exception):
    pass


class JobRegistry:
    def __init__(self, max_jobs: int, max_per_user: int = MAX_JOBS_PER_USER) -> None:
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs
        self._max_per_user = max_per_user

    def create(self, service: str, owner: str) -> Job:
        self._expire()
        if len(self._jobs) >= self._max_jobs:
            raise TooManyJobs("gateway")
        if sum(1 for job in self._jobs.values() if job.owner == owner) >= self._max_per_user:
            raise TooManyJobs("user")
        job = Job(id=secrets.token_urlsafe(24), service=service, owner=owner)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        """The job, only for its owner: anyone else gets None, as for an unknown id."""
        self._expire()
        job = self._jobs.get(job_id)
        return job if job is not None and job.owner == owner else None

    def remove(self, job: Job) -> None:
        self._jobs.pop(job.id, None)

    def __len__(self) -> int:
        return len(self._jobs)

    def _expire(self) -> None:
        deadline = time.monotonic() - ATTACH_TIMEOUT_SECONDS
        for job in list(self._jobs.values()):
            # A job whose upload never started is abandoned once the attach window passes.
            if job.created < deadline and not job.upload_started.is_set():
                job.pipe.close()
                self._jobs.pop(job.id, None)


class _Recorder:
    """Wraps the pipe's `send` to learn what the service answered, for the upload's reply."""

    def __init__(self, send: Any) -> None:
        self._send = send
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = bytearray()
        self.completed = False

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            if self.status != 200 and len(self.body) < MAX_PROBLEM_BYTES:
                self.body += message.get("body", b"")[: MAX_PROBLEM_BYTES - len(self.body)]
            if not message.get("more_body", False):
                self.completed = True
        await self._send(message)


def archive_name(content_disposition: str) -> str | None:
    """The file name from `filename*=UTF-8''...` (preferred) or `filename="..."`."""
    star = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
    if star:
        return unquote(star.group(1).strip())
    plain = re.search(r'filename="([^"]*)"', content_disposition, re.IGNORECASE)
    return plain.group(1) if plain else None


# Replaced on the download: it is a navigation on the portal origin, with the session
# cookie, so whatever a service answers must never render or run there.
_DOWNLOAD_REPLACED = frozenset(
    {
        b"set-cookie",
        b"content-disposition",
        b"content-security-policy",
        b"x-content-type-options",
        b"cache-control",
    }
)


def download_headers(start: Message) -> list[tuple[bytes, bytes]]:
    """The service's response headers, made safe for the download frame.

    A 200 is always saved as a file (the service's file name kept); anything else (a
    problem) may show in the hidden frame, but sandboxed and never sniffed as HTML.
    """
    headers: list[tuple[bytes, bytes]] = start["headers"]
    kept = [(name, value) for name, value in headers if name.lower() not in _DOWNLOAD_REPLACED]
    if start["status"] == 200:
        disposition = next(
            (value for name, value in headers if name.lower() == b"content-disposition"), b""
        )
        _, separator, parameters = disposition.partition(b";")
        kept.append((b"content-disposition", b"attachment" + separator + parameters))
    kept += [
        (b"content-security-policy", b"sandbox"),
        (b"x-content-type-options", b"nosniff"),
        (b"cache-control", b"no-store"),
    ]
    return kept


class BrowserJobs:
    """The three job endpoints. Plain ASGI where bodies stream, so nothing buffers."""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionSigner,
        proxy: ServiceProxy,
        registry: JobRegistry,
        users: UserGate | None = None,
        quota: QuotaService | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._users = users
        self._quota = quota
        self._proxy = proxy
        self._registry = registry

    # --------------------------------------------------------------------- helpers

    async def _owner(self, scope: Scope) -> tuple[str | None, str | None]:
        """(owner id, session token) from the session cookie; None if not signed in."""
        token = Request(scope).cookies.get(SESSION_COOKIE)
        session = self._sessions.verify(token)
        if session is None:
            return None, token
        if self._users is not None and await self._users.check(session) is None:
            return None, token  # disabled or signed out since
        return session.owner, token

    async def _guard(self, scope: Scope, *, state_changing: bool) -> tuple[str | None, str | None]:
        """The signed-in user, or (None, problem code) if the request must be refused."""
        user, token = await self._owner(scope)
        if user is None:
            return None, "unauthorized"
        if state_changing:
            headers = Headers(scope=scope)
            if not same_origin(headers, scope.get("scheme", "http")):
                return None, "forbidden"
            if not self._sessions.check_csrf(token, headers.get("x-csrf-token")):
                return None, "forbidden"
        return user, None

    @staticmethod
    def _request_id(scope: Scope) -> str:
        return str(scope["state"]["request_id"])

    # --------------------------------------------------------------------- endpoints

    async def create(self, scope: Scope, receive: Receive, send: Send) -> None:
        user, refusal = await self._guard(scope, state_changing=True)
        if user is None:
            await self._problem(scope, receive, send, refusal or "unauthorized")
            return
        try:
            body = json.loads(await _read_small_body(receive) or b"{}")
            service = body.get("service") if isinstance(body, dict) else None
        except ValueError:
            service = None
        if not isinstance(service, str) or service not in self._settings.upstreams:
            await self._problem(scope, receive, send, "not-found", "No such service.")
            return
        refused = await self._quota.refusal(user) if self._quota is not None else None
        if refused is not None:
            wait, detail = refused
            await self._problem(
                scope, receive, send, "quota-exceeded", detail, headers={"Retry-After": str(wait)}
            )
            return
        try:
            job = self._registry.create(service, user)
        except TooManyJobs:
            await self._problem(scope, receive, send, "overloaded", headers={"Retry-After": "5"})
            return
        base = f"/zip/jobs/{job.id}"
        response = JSONResponse(
            {"id": job.id, "upload": f"{base}/files", "download": f"{base}/archive"},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)

    async def download(self, scope: Scope, receive: Receive, send: Send) -> None:
        user, refusal = await self._guard(scope, state_changing=False)
        job = self._job(scope, user)
        if user is None or job is None or job.download_attached.is_set():
            await self._problem(scope, receive, send, refusal or "not-found")
            return
        job.download_attached.set()
        try:
            await asyncio.wait_for(job.upload_started.wait(), ATTACH_TIMEOUT_SECONDS)
        except TimeoutError:
            self._registry.remove(job)
            await self._problem(scope, receive, send, "request-timeout")
            return

        # Relay the exchange's messages; a disconnect cancels the whole job at once.
        disconnected = asyncio.ensure_future(_wait_for_disconnect(receive))
        try:
            while True:
                next_message = asyncio.ensure_future(job.pipe.receive())
                done, _ = await asyncio.wait(
                    {next_message, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    next_message.cancel()
                    job.cancel()
                    return
                message = next_message.result()
                if message is None:
                    # The exchange ended without completing the response: return without
                    # the final body message, so the browser sees a failed download.
                    return
                if message["type"] == "http.response.start":
                    message = {**message, "headers": download_headers(message)}
                try:
                    await send(message)
                except OSError:
                    job.cancel()
                    return
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    return
        finally:
            disconnected.cancel()

    async def upload(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.monotonic()
        user, refusal = await self._guard(scope, state_changing=True)
        job = self._job(scope, user)
        if user is None or job is None or job.upload_started.is_set():
            await self._problem(scope, receive, send, refusal or "not-found")
            return
        try:
            await asyncio.wait_for(job.download_attached.wait(), ATTACH_TIMEOUT_SECONDS)
        except TimeoutError:
            self._registry.remove(job)
            await self._problem(scope, receive, send, "request-timeout")
            return
        job.upload_started.set()

        upstream = self._settings.upstreams[job.service]
        stats = Stats()
        recorder = _Recorder(job.pipe.send)
        job.exchange = asyncio.ensure_future(
            self._proxy.forward(
                scope, receive, recorder, upstream, self._request_id(scope), b"/api/v1/zip", stats
            )
        )
        try:
            await job.exchange
        except asyncio.CancelledError:
            if not job.cancelled:
                raise  # this request itself is being cancelled (shutdown)
            stats.outcome, stats.code = "client_disconnected", "download-cancelled"
        finally:
            await job.pipe.finish()
            self._registry.remove(job)
            if self._quota is not None:
                self._quota.record(job.owner, stats.bytes_in + stats.bytes_out)
            log_request("POST", "browser", job.service, user, stats, started, job=job.id)

        if job.cancelled:
            outcome = "cancelled"
        elif recorder.completed and recorder.status == 200:
            outcome = "completed"
        elif recorder.status is not None and recorder.status != 200 and recorder.completed:
            outcome = "rejected"
        else:
            outcome = "aborted"
        result: dict[str, Any] = {
            "outcome": outcome,
            "status": recorder.status,
            "code": stats.code,
            "bytes_in": stats.bytes_in,
            "bytes_out": stats.bytes_out,
            "filename": archive_name(recorder.headers.get("content-disposition", "")),
        }
        if outcome == "rejected":
            try:
                result["problem"] = json.loads(bytes(recorder.body))
            except ValueError:
                result["problem"] = None
        # If the uploader has gone, the outcome is still in the log line above.
        with contextlib.suppress(OSError):
            await JSONResponse(result, headers={"Cache-Control": "no-store"})(scope, receive, send)

    # --------------------------------------------------------------------- internals

    def _job(self, scope: Scope, user: str | None) -> Job | None:
        job_id = scope.get("path_params", {}).get("job_id", "")
        if user is None or not _JOB_ID.fullmatch(job_id):
            return None
        return self._registry.get(job_id, user)

    async def _problem(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        response: Response = problem_response(
            code, self._request_id(scope), detail=detail, headers=headers
        )
        with contextlib.suppress(OSError):
            await response(scope, receive, send)


async def _read_small_body(receive: Receive, limit: int = 4096) -> bytes:
    """A small JSON body; anything longer than `limit` is refused as invalid."""
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b""
        chunk: bytes = message.get("body", b"")
        body += chunk
        if len(body) > limit:
            return b"!"  # not JSON: rejected by the caller
        if not message.get("more_body", False):
            return body


class AsgiEndpoint:
    """Wraps a handler so Starlette mounts it as raw ASGI (Starlette would treat a bound
    method as a request/response function, which buffers)."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


async def _wait_for_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass
