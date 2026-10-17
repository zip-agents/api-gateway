"""Smoke tests for every zip service, run through the gateway exactly as clients use it.

Each test runs once per service in $SERVICES (default: claude,grok). They check the parts
of the contract a deploy most often breaks, plus the gateway hop itself (auth, request
IDs, streaming), and finish in seconds. Exhaustive contract tests live in each service's
own repository.

Environment:
    GATEWAY_URL   base URL of the gateway (default http://gateway:8000)
    API_KEY       a key from GATEWAY_API_KEYS
    SERVICES      comma-separated service names
    LARGE_UPLOAD_MIB  size of the streamed upload test (default 64)
    SSL_VERIFY    "false" to accept any certificate (e.g. a self-signed local TLS setup)
"""

import hashlib
import io
import os
import subprocess
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
SERVICES = [s.strip() for s in os.environ.get("SERVICES", "claude,grok").split(",") if s.strip()]
LARGE_UPLOAD_MIB = int(os.environ.get("LARGE_UPLOAD_MIB", "64"))
SSL_VERIFY = os.environ.get("SSL_VERIFY", "true").lower() != "false"

pytestmark = pytest.mark.parametrize("service", SERVICES)


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    if not API_KEY:
        pytest.exit("API_KEY is not set", returncode=2)
    with httpx.Client(
        base_url=GATEWAY_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=httpx.Timeout(60, connect=5),
        verify=SSL_VERIFY,
    ) as client:
        yield client


def problem(response: httpx.Response) -> dict[str, object]:
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, object] = response.json()
    return body


def test_ready(client: httpx.Client, service: str) -> None:
    response = client.get(f"/{service}/health/ready")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}


def test_contract_example(client: httpx.Client, service: str, tmp_path: Path) -> None:
    """Contract §13, checked with Python zipfile, unzip -t and 7z t, byte for byte."""
    photo = os.urandom(200_000)
    response = client.post(
        f"/{service}/api/v1/zip",
        data={"filename": "quarterly report"},
        files=[
            ("files", ("a.txt", b"hello\n")),
            ("files", ("a.txt", b"hello\n")),
            ("files", ("photo.jpg", photo)),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"quarterly report.zip\"; "
        "filename*=UTF-8''quarterly%20report.zip"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-length" not in response.headers

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["a.txt", "a (1).txt", "photo.jpg"]
    assert archive.testzip() is None
    assert archive.read("photo.jpg") == photo
    path = tmp_path / "out.zip"
    path.write_bytes(response.content)
    subprocess.run(["unzip", "-tq", str(path)], check=True, capture_output=True)
    subprocess.run(["7z", "t", str(path)], check=True, capture_output=True)


def test_request_id_reaches_the_service_and_back(client: httpx.Client, service: str) -> None:
    request_id = f"smoke-{uuid.uuid4()}"
    response = client.post(
        f"/{service}/api/v1/zip",
        files=[("files", ("a.txt", b"x"))],
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"json": {}}, 415, "unsupported-media-type"),
        ({"data": {"note": "no files"}, "files": [("files", ("", b""))]}, 400, "no-files"),
        ({"files": [("other", ("a.txt", b"x"))]}, 400, "unexpected-file-field"),
        (
            {"data": {"filename": "bad/name"}, "files": [("files", ("a.txt", b"x"))]},
            422,
            "invalid-archive-name",
        ),
    ],
)
def test_errors_are_service_problem_details(
    client: httpx.Client, service: str, kwargs: dict[str, object], status: int, code: str
) -> None:
    response = client.post(f"/{service}/api/v1/zip", **kwargs)  # type: ignore[arg-type]
    assert response.status_code == status, response.text
    body = problem(response)
    assert body["code"] == code
    assert body["type"] == f"urn:zip-service:problem:{code}"
    assert response.headers["x-request-id"]


def test_unknown_service_path_is_404(client: httpx.Client, service: str) -> None:
    response = client.get(f"/{service}/no/such/path")
    assert response.status_code == 404
    assert problem(response)["code"] == "not-found"


def test_gateway_rejects_missing_or_wrong_key(service: str) -> None:
    for headers in ({}, {"Authorization": "Bearer not-a-real-key-0000000000000000000000"}):
        response = httpx.post(
            f"{GATEWAY_URL}/{service}/api/v1/zip",
            files=[("files", ("a.txt", b"x"))],
            headers=headers,
            verify=SSL_VERIFY,
        )
        assert response.status_code == 401
        assert problem(response)["type"] == "urn:zip-gateway:problem:unauthorized"


def test_large_upload_streams_through_the_gateway(service: str, tmp_path: Path) -> None:
    """An upload far larger than the 16 MiB output buffer, sent by a full-duplex client.

    The data is incompressible, so the archive is as large as the upload: this only works
    if the service and the gateway both stream in both directions (contract §4.1).
    """
    source = tmp_path / "big.bin"
    digest = hashlib.sha256()
    with source.open("wb") as handle:
        for _ in range(LARGE_UPLOAD_MIB):
            chunk = os.urandom(1024 * 1024)
            digest.update(chunk)
            handle.write(chunk)
    out = tmp_path / "big.zip"
    result = subprocess.run(
        [
            "curl", "-sS", "--fail-with-body", "--max-time", "300",
            *([] if SSL_VERIFY else ["--insecure"]),
            "-H", f"Authorization: Bearer {API_KEY}",
            "-F", f"files=@{source};filename=big.bin",
            "-o", str(out),
            f"{GATEWAY_URL}/{service}/api/v1/zip",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    subprocess.run(["unzip", "-tq", str(out)], check=True, capture_output=True)
    with zipfile.ZipFile(out) as archive, archive.open("big.bin") as entry:
        extracted = hashlib.sha256()
        while chunk := entry.read(1024 * 1024):
            extracted.update(chunk)
    assert extracted.hexdigest() == digest.hexdigest()


def test_docs_endpoints(client: httpx.Client, service: str) -> None:
    """Contract 1.1.0 §14. Skipped for services still on contract 1.0.0."""
    response = client.get(f"/{service}/docs/manifest.json")
    if response.status_code == 404:
        pytest.skip(f"{service} does not implement contract §14 (docs endpoints) yet")
    assert response.status_code == 200
    manifest = response.json()
    slugs = [document["slug"] for document in manifest["documents"]]
    assert slugs[:2] == ["readme", "agents"]
    for document in manifest["documents"]:
        doc = client.get(f"/{service}{document['path']}")
        assert doc.status_code == 200, document
        assert doc.headers["content-type"].startswith("text/markdown")
        assert len(doc.content) <= 1024 * 1024
    if "openapi" in manifest:  # §14.4, optional
        spec = client.get(f"/{service}{manifest['openapi']}")
        assert spec.status_code == 200
        document = spec.json()
        assert document["openapi"].startswith("3.")
        assert "/api/v1/zip" in document["paths"]


@pytest.mark.skipif(not GATEWAY_URL.startswith("https://"), reason="plain HTTP deployment")
def test_https_sets_hsts(client: httpx.Client, service: str) -> None:
    response = client.get(f"/{service}/health/live")
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_browser_upload_job_streams_at_any_size(service: str, tmp_path: Path) -> None:
    """The upload page's flow (gateway browser jobs): sign in, create a job, then download
    and upload concurrently. The upload is far larger than the service's output buffer
    and incompressible, so it only succeeds if the two halves really stream together."""
    import re
    import threading

    source = tmp_path / "big.bin"
    digest = hashlib.sha256()
    with source.open("wb") as handle:
        for _ in range(LARGE_UPLOAD_MIB):
            chunk = os.urandom(1024 * 1024)
            digest.update(chunk)
            handle.write(chunk)

    with httpx.Client(base_url=GATEWAY_URL, verify=SSL_VERIFY, timeout=300) as browser:
        signed_in = browser.post("/docs/login", data={"api_key": API_KEY, "next": "/docs/zip"})
        assert signed_in.status_code == 303, signed_in.text
        page = browser.get("/docs/zip")
        csrf = re.search(r'data-csrf="([^"]+)"', page.text)
        assert csrf, "no CSRF token on the upload page"
        same_origin = {"Origin": GATEWAY_URL, "X-CSRF-Token": csrf.group(1)}

        created = browser.post("/docs/zip/jobs", json={"service": service}, headers=same_origin)
        assert created.status_code == 201, created.text
        job = created.json()

        archive = tmp_path / "browser.zip"
        failure: list[BaseException] = []

        def download() -> None:
            try:
                with browser.stream("GET", job["download"]) as response, archive.open("wb") as out:
                    assert response.status_code == 200, response.read()
                    for chunk in response.iter_bytes():
                        out.write(chunk)
            except BaseException as exc:  # reported by the main thread
                failure.append(exc)

        downloader = threading.Thread(target=download)
        downloader.start()
        with source.open("rb") as handle:
            uploaded = browser.post(
                job["upload"], files=[("files", ("big.bin", handle))], headers=same_origin
            )
        downloader.join(timeout=300)
        assert not failure, failure
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["outcome"] == "completed", uploaded.json()

    with zipfile.ZipFile(archive) as zipped, zipped.open("big.bin") as entry:
        extracted = hashlib.sha256()
        while chunk := entry.read(1024 * 1024):
            extracted.update(chunk)
    assert extracted.hexdigest() == digest.hexdigest()
