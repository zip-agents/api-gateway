"""Browser upload jobs: two half-duplex requests joined into one full-duplex exchange."""

import asyncio
import json
from typing import Any

import pytest

from gateway import browser_jobs
from gateway.app import Gateway
from gateway.auth import KeyRing, SessionSigner
from gateway.browser_jobs import JobRegistry, archive_name
from gateway.config import Settings
from tests.support import (
    API_KEY,
    SESSION_SECRET,
    AsgiClient,
    FakeUpstream,
    ServerConn,
    make_settings,
)

pytestmark = pytest.mark.anyio

OTHER_KEY = "other-key-0123456789abcdef0123456789abcdef"
BOUNDARY = "zipboundary"
DISPOSITION = "attachment; filename=\"zipped_files.zip\"; filename*=UTF-8''zipped_files.zip"


def settings_for(fake: FakeUpstream, **env: str) -> Settings:
    return make_settings(
        GATEWAY_UPSTREAMS=f"claude={fake.url}",
        GATEWAY_API_KEYS=f"tester:{API_KEY},other:{OTHER_KEY}",
        **env,
    )


class Browser:
    """One signed-in browser: its cookie, CSRF token and same-origin headers."""

    def __init__(self, settings: Settings, user: str = "tester") -> None:
        signer = SessionSigner(SESSION_SECRET, 3600, KeyRing(settings.api_keys))
        self.session = signer.issue(user)
        self.csrf = signer.csrf_token(self.session)

    def headers(self, *, csrf: bool = True, origin: str = "http://gw") -> list[tuple[str, str]]:
        headers = [("Host", "gw"), ("Cookie", f"gw_docs_session={self.session}")]
        if origin:
            headers.append(("Origin", origin))
        if csrf:
            headers.append(("X-CSRF-Token", self.csrf))
        return headers


async def call(app: Gateway, client: AsgiClient) -> None:
    await asyncio.wait_for(app(client.scope, client.receive, client.send), timeout=10)


async def create_job(
    app: Gateway, browser: Browser, service: str = "claude", **header_options: Any
) -> AsgiClient:
    client = AsgiClient(
        "POST",
        "/docs/zip/jobs",
        [*browser.headers(**header_options), ("Content-Type", "application/json")],
    )
    client.feed(json.dumps({"service": service}).encode())
    await call(app, client)
    return client


def multipart(*parts: tuple[str, bytes]) -> list[bytes]:
    """Body chunks: one per file part, then the closing boundary."""
    chunks = [
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + b"\r\n"
        for name, data in parts
    ]
    return [*chunks, f"--{BOUNDARY}--\r\n".encode()]


def upload_client(browser: Browser, url: str) -> AsgiClient:
    return AsgiClient(
        "POST",
        url,
        [
            *browser.headers(),
            ("Content-Type", f"multipart/form-data; boundary={BOUNDARY}"),
            ("Transfer-Encoding", "chunked"),
        ],
    )


def outcome(client: AsgiClient) -> dict[str, Any]:
    assert client.status == 200, client.body
    result: dict[str, Any] = json.loads(client.body)
    return result


async def serve_archive(conn: ServerConn, seen: dict[str, Any]) -> None:
    request = await conn.read_request()
    seen["target"] = request.target
    seen["headers"] = {k.decode(): v.decode() for k, v in request.headers}
    seen["body"] = await conn.read_body()
    await conn.respond(
        200, [("Content-Type", "application/zip"), ("Content-Disposition", DISPOSITION)]
    )
    await conn.write(b"PK\x03\x04")
    await conn.write(b"archive")
    await conn.end()


async def test_job_streams_the_upload_to_the_service_and_the_archive_to_the_download() -> None:
    seen: dict[str, Any] = {}

    async def handler(conn: ServerConn) -> None:
        await serve_archive(conn, seen)

    async with FakeUpstream(handler) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)

        download = AsgiClient("GET", job["download"], browser.headers(csrf=False))
        upload = upload_client(browser, job["upload"])
        for chunk in multipart(("a.txt", b"hello"), ("b.txt", b"world")):
            upload.feed(chunk, more=True)
        upload.feed(b"")
        await asyncio.gather(call(app, download), call(app, upload))

    assert seen["target"] == b"/api/v1/zip"
    assert seen["headers"]["content-type"] == f"multipart/form-data; boundary={BOUNDARY}"
    assert "cookie" not in seen["headers"]
    assert "x-csrf-token" not in seen["headers"]
    assert b'filename="a.txt"' in seen["body"]

    assert download.status == 200
    assert download.headers["content-disposition"] == DISPOSITION
    assert download.body == b"PK\x03\x04archive"
    assert download.completed

    result = outcome(upload)
    assert result["outcome"] == "completed"
    assert result["filename"] == "zipped_files.zip"
    assert result["bytes_out"] == len(b"PK\x03\x04archive")


async def test_archive_bytes_reach_the_download_while_the_upload_is_still_going() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_chunk()  # the first file only
        await conn.respond(200, [("Content-Type", "application/zip")])
        await conn.write(b"early")
        await conn.read_body()
        await conn.write(b"late")
        await conn.end()

    async with FakeUpstream(handler) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)
        download = AsgiClient("GET", job["download"], browser.headers(csrf=False))
        upload = upload_client(browser, job["upload"])
        first, *rest = multipart(("a.bin", b"x" * 1000), ("b.bin", b"y" * 1000))
        upload.feed(first, more=True)
        tasks = [
            asyncio.ensure_future(call(app, download)),
            asyncio.ensure_future(call(app, upload)),
        ]
        # The rest of the upload is only sent once archive bytes have been downloaded: a
        # half-duplex design would deadlock here.
        await asyncio.wait_for(download.body_received.wait(), timeout=5)
        assert download.body == b"early"
        for chunk in rest:
            upload.feed(chunk, more=True)
        upload.feed(b"")
        await asyncio.gather(*tasks)

    assert download.body == b"earlylate"
    assert outcome(upload)["outcome"] == "completed"


async def test_a_rejection_is_reported_to_the_uploader() -> None:
    problem = {"type": "urn:zip-service:problem:no-files", "title": "No files", "code": "no-files"}
    body = json.dumps(problem).encode()

    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(
            400,
            [("Content-Type", "application/problem+json"), ("Content-Length", str(len(body)))],
        )
        await conn.write(body)
        await conn.end()

    async with FakeUpstream(handler) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)
        download = AsgiClient("GET", job["download"], browser.headers(csrf=False))
        upload = upload_client(browser, job["upload"])
        upload.feed(f"--{BOUNDARY}--\r\n".encode())
        await asyncio.gather(call(app, download), call(app, upload))

    result = outcome(upload)
    assert result["outcome"] == "rejected"
    assert result["status"] == 400
    assert result["problem"]["code"] == "no-files"
    assert download.status == 400


async def test_a_mid_stream_failure_truncates_the_download() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(200, [("Content-Type", "application/zip")])
        await conn.write(b"partial")
        await asyncio.sleep(0.1)
        conn.close()

    async with FakeUpstream(handler) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)
        download = AsgiClient("GET", job["download"], browser.headers(csrf=False))
        upload = upload_client(browser, job["upload"])
        for chunk in multipart(("a.txt", b"hello")):
            upload.feed(chunk, more=True)
        upload.feed(b"")
        await asyncio.gather(call(app, download), call(app, upload))

    assert download.body == b"partial"
    assert not download.completed  # the browser marks the download failed
    result = outcome(upload)
    assert result["outcome"] == "aborted"
    assert result["code"] == "upstream-error"


async def test_cancelling_the_download_stops_the_exchange() -> None:
    async def handler(conn: ServerConn) -> None:
        await conn.read_request()
        await conn.read_body()
        await conn.respond(200, [("Content-Type", "application/zip")])
        while True:  # until the gateway hangs up
            await conn.write(b"z" * 65536)
            await asyncio.sleep(0.01)

    async with FakeUpstream(handler) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)
        download = AsgiClient("GET", job["download"], browser.headers(csrf=False))
        upload = upload_client(browser, job["upload"])
        for chunk in multipart(("a.txt", b"hello")):
            upload.feed(chunk, more=True)
        upload.feed(b"")
        tasks = [
            asyncio.ensure_future(call(app, download)),
            asyncio.ensure_future(call(app, upload)),
        ]
        await asyncio.wait_for(download.body_received.wait(), timeout=5)
        download.disconnect()
        await asyncio.gather(*tasks)
        await asyncio.wait_for(fake.handled.wait(), timeout=5)  # upstream connection closed

    assert outcome(upload)["outcome"] == "cancelled"


async def test_creating_a_job_needs_a_session_csrf_and_same_origin() -> None:
    async with FakeUpstream(serve_nothing) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        assert (await create_job(app, browser)).status == 201
        assert (await create_job(app, browser, csrf=False)).status == 403
        assert (await create_job(app, browser, origin="https://evil.example")).status == 403
        assert (await create_job(app, browser, origin="")).status == 403
        assert (await create_job(app, browser, service="nope")).status == 404

        anonymous = AsgiClient("POST", "/docs/zip/jobs", [("Host", "gw"), ("Origin", "http://gw")])
        anonymous.feed(b'{"service": "claude"}')
        await call(app, anonymous)
        assert anonymous.status == 401


async def test_a_job_belongs_to_the_user_who_created_it() -> None:
    async with FakeUpstream(serve_nothing) as fake:
        app = Gateway(settings_for(fake))
        owner, other = Browser(app._settings), Browser(app._settings, user="other")
        job = json.loads((await create_job(app, owner)).body)
        stolen = AsgiClient("GET", job["download"], other.headers(csrf=False))
        await call(app, stolen)
        assert stolen.status == 404
        guessed = AsgiClient(
            "GET", "/docs/zip/jobs/" + "A" * 32 + "/archive", owner.headers(csrf=False)
        )
        await call(app, guessed)
        assert guessed.status == 404


async def test_an_upload_without_its_download_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_jobs, "ATTACH_TIMEOUT_SECONDS", 0.2)
    async with FakeUpstream(serve_nothing) as fake:
        app = Gateway(settings_for(fake))
        browser = Browser(app._settings)
        job = json.loads((await create_job(app, browser)).body)
        upload = upload_client(browser, job["upload"])
        upload.feed(f"--{BOUNDARY}--\r\n".encode())
        await call(app, upload)
        assert upload.status == 408
        assert fake.connections == 0


async def test_job_count_is_capped() -> None:
    async with FakeUpstream(serve_nothing) as fake:
        app = Gateway(settings_for(fake, GATEWAY_BROWSER_JOBS_MAX="1"))
        browser = Browser(app._settings)
        assert (await create_job(app, browser)).status == 201
        refused = await create_job(app, browser)
        assert refused.status == 503
        assert refused.headers["retry-after"] == "5"


def test_per_user_cap_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = JobRegistry(max_jobs=10, max_per_user=2)
    registry.create("claude", "a")
    registry.create("claude", "a")
    with pytest.raises(browser_jobs.TooManyJobs):
        registry.create("claude", "a")
    registry.create("claude", "b")  # other users are unaffected
    monkeypatch.setattr(browser_jobs, "ATTACH_TIMEOUT_SECONDS", -1.0)
    registry.create("claude", "c")  # creating expires the abandoned jobs first
    assert len(registry) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (DISPOSITION, "zipped_files.zip"),
        ("attachment; filename=\"_.zip\"; filename*=UTF-8''%C3%A9t%C3%A9.zip", "été.zip"),
        ('attachment; filename="plain.zip"', "plain.zip"),
        ("", None),
    ],
)
def test_archive_name(header: str, expected: str | None) -> None:
    assert archive_name(header) == expected


async def serve_nothing(conn: ServerConn) -> None:
    raise AssertionError("the service must not be contacted")
