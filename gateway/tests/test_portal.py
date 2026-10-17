"""The gateway app end to end in-process: routing, request IDs, health and the docs portal."""

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from gateway.app import Gateway
from gateway.docs import DocsSite
from gateway.service_docs import InvalidDocs, parse_manifest
from tests.support import API_KEY, FakeUpstream, ServerConn, make_settings

pytestmark = pytest.mark.anyio

MANIFEST = {
    "service": "zip-service",
    "version": "1.1.0",
    "revision": "3f2c9e1aa0b1c2d3",
    "contract_version": "1.1.0",
    "openapi": "/docs/openapi.json",
    "documents": [
        {"slug": "readme", "title": "README", "path": "/docs/README.md"},
        {"slug": "agents", "title": "AGENTS", "path": "/docs/AGENTS.md"},
    ],
}
FILES = {
    b"/docs/manifest.json": json.dumps(MANIFEST).encode(),
    b"/docs/README.md": (
        b"# Claude's zip service\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n"
    ),
    b"/docs/AGENTS.md": b"# Agents\n",
}
# The service's own document: the contract, except that it never documents 413.
SERVICE_SPEC = copy.deepcopy(DocsSite().contract_spec)
del SERVICE_SPEC["paths"]["/api/v1/zip"]["post"]["responses"]["413"]
FILES[b"/docs/openapi.json"] = json.dumps(SERVICE_SPEC).encode()


async def serve_docs(conn: ServerConn) -> None:
    request = await conn.read_request()
    await conn.read_body()
    body = FILES.get(request.target)
    if request.target == b"/health/ready":
        body = b'{"status":"ready"}'
    if body is None:
        await conn.respond(404, [("Content-Length", "0")])
    else:
        await conn.respond(200, [("Content-Length", str(len(body)))])
        await conn.write(body)
    await conn.end()


@pytest.fixture
async def service() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(serve_docs) as fake:
        yield fake


def client_for(upstreams: str) -> httpx.AsyncClient:
    gateway = Gateway(make_settings(GATEWAY_UPSTREAMS=upstreams), DocsSite())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gw")


async def sign_in(client: httpx.AsyncClient) -> None:
    response = await client.post("/docs/login", data={"api_key": API_KEY, "next": "/docs"})
    assert response.status_code == 303
    assert response.headers["location"] == "/docs"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/docs" in cookie


async def test_health_needs_no_auth() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/health/ready")).json() == {"status": "ready"}


async def test_request_id_is_echoed_or_generated() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        echoed = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
        assert echoed.headers["x-request-id"] == "abc-123"
        generated = await client.get("/health/live", headers={"X-Request-ID": "bad id!"})
        assert len(generated.headers["x-request-id"]) == 36


async def test_unknown_paths_are_problem_details() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        response = await client.get("/nope/api/v1/zip", headers={"X-Request-ID": "r1"})
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "not-found"
        assert response.json()["request_id"] == "r1"
        response = await client.put("/health/live")
        assert response.status_code == 405
        assert response.json()["code"] == "method-not-allowed"


async def test_service_routes_need_a_bearer_key_not_the_docs_cookie(
    service: FakeUpstream,
) -> None:
    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        # Even sent explicitly, the docs session cookie is no substitute for a bearer key.
        session = client.cookies["gw_docs_session"]
        response = await client.get(
            "/claude/health/ready", headers={"Cookie": f"gw_docs_session={session}"}
        )
        assert response.status_code == 401
        response = await client.get(
            "/claude/health/ready", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}


async def test_docs_redirect_to_login_until_signed_in(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url}") as client:
        for path in (
            "/docs",
            "/docs/api",
            "/docs/api/claude",
            "/docs/compare",
            "/docs/zip",
            "/docs/pages/contract",
            "/docs/services/claude/readme",
        ):
            response = await client.get(path)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/docs/login?next=")
        assert (await client.get("/docs/openapi.yaml")).status_code == 401
        assert (await client.get("/docs/openapi/contract.json")).status_code == 401
        assert (await client.get("/docs/openapi/claude/service.json")).status_code == 401


async def test_login_rejects_a_wrong_key_and_unsafe_redirects() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        response = await client.post("/docs/login", data={"api_key": "wrong"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        response = await client.post(
            "/docs/login", data={"api_key": API_KEY, "next": "https://evil.example/docs"}
        )
        assert response.headers["location"] == "/docs"


async def test_home_shows_status_build_and_service_docs(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url},grok=http://127.0.0.1:1") as client:
        await sign_in(client)
        response = await client.get("/docs")
    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    page = response.text
    assert "/claude/api/v1/zip" in page
    assert "1.1.0 3f2c9e1aa0b1" in page  # version and short revision
    assert 'href="/docs/services/claude/readme"' in page
    assert 'href="/docs/api/claude"' in page  # each endpoint links to its reference
    assert 'href="/docs/api/grok"' in page  # even when its docs are unavailable
    assert "unreachable" in page  # grok is not running
    assert "unavailable: service unreachable" in page
    assert "own document" in page  # claude publishes an OpenAPI document
    assert "contract only" in page  # grok cannot be reached


async def test_service_document_is_rendered_with_html_escaped(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        response = await client.get("/docs/services/claude/readme")
        assert response.status_code == 200
        assert "<h1>Claude's zip service</h1>" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text
        assert "<table>" in response.text
        missing = await client.get("/docs/services/claude/nope")
        assert missing.status_code == 404
        unknown_service = await client.get("/docs/services/nope/readme")
        assert unknown_service.status_code == 404


async def test_service_docs_are_cached(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        await client.get("/docs/services/claude/readme")
        after_first = service.connections
        await client.get("/docs/services/claude/agents")
        assert service.connections == after_first


async def test_gateway_pages_and_api_reference(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        contract = await client.get("/docs/pages/contract")
        assert contract.status_code == 200
        assert "Documentation endpoints" in contract.text
        api = await client.get("/docs/api")
        assert 'integrity="sha384-' in api.text
        assert "connect-src 'self'" in api.headers["content-security-policy"]
        assert 'data-spec="/docs/openapi/contract.json"' in api.text
        assert (await client.get("/docs/api/nope")).status_code == 404
        spec = await client.get("/docs/openapi.yaml")
        assert spec.status_code == 200
        assert "/api/v1/zip:" in spec.text


async def test_service_reference_shows_its_own_document(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url},grok=http://127.0.0.1:1") as client:
        await sign_in(client)
        own = await client.get("/docs/api/claude")
        assert 'data-spec="/docs/openapi/claude/service.json"' in own.text
        assert "claude's own document" in own.text
        contract = await client.get("/docs/api/claude?source=contract")
        assert 'data-spec="/docs/openapi/claude/contract.json"' in contract.text
        # grok publishes nothing (here it is unreachable): its page shows the contract.
        fallback = await client.get("/docs/api/grok")
        assert 'data-spec="/docs/openapi/grok/contract.json"' in fallback.text
        assert "own document is unavailable" in fallback.text


async def test_documents_are_prepared_for_the_gateway(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url},grok=http://127.0.0.1:1") as client:
        await sign_in(client)
        own = (await client.get("/docs/openapi/claude/service.json")).json()
        assert own["servers"] == [
            {"url": "http://gw/claude", "description": "claude, through the gateway"}
        ]
        assert "413" not in own["paths"]["/api/v1/zip"]["post"]["responses"]  # as published
        assert "401" in own["paths"]["/api/v1/zip"]["post"]["responses"]  # added
        curl = own["paths"]["/api/v1/zip"]["post"]["x-codeSamples"][0]["source"]
        assert '"http://gw/claude/api/v1/zip"' in curl
        assert "Published by claude itself" in own["info"]["description"]

        contract = (await client.get("/docs/openapi/contract.json")).json()
        assert [s["url"] for s in contract["servers"]] == ["http://gw/claude", "http://gw/grok"]

        assert (await client.get("/docs/openapi/grok/service.json")).status_code == 404
        assert (await client.get("/docs/openapi/grok/contract.json")).status_code == 200
        assert (await client.get("/docs/openapi/claude/other.json")).status_code == 404


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "zip.example.com"},
            "https://zip.example.com/claude",
        ),
        ({"X-Forwarded-Host": 'bad host"><script>'}, "/claude"),  # never reflected
        ({"X-Forwarded-Proto": "javascript"}, "/claude"),
    ],
)
async def test_server_url_follows_the_public_origin(
    service: FakeUpstream, headers: dict[str, str], expected: str
) -> None:
    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        document = (await client.get("/docs/openapi/claude/service.json", headers=headers)).json()
        assert document["servers"][0]["url"] == expected


async def test_compare_page_reports_differences(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url},grok=http://127.0.0.1:1") as client:
        await sign_in(client)
        page = (await client.get("/docs/compare")).text
    assert '<h2 id="claude">' in page
    assert "1 difference." in page
    assert "<code>POST /api/v1/zip</code></td><td>response status</td><td>413</td>" in page
    assert '<h2 id="grok">' in page
    assert "No OpenAPI document published" in page


async def test_logout_clears_the_session() -> None:
    async with client_for("claude=http://127.0.0.1:1") as client:
        await sign_in(client)
        response = await client.post("/docs/logout")
        assert response.status_code == 303
        assert "max-age=0" in response.headers["set-cookie"].lower()


def test_shipped_docs_exist() -> None:
    root = Path(__file__).resolve().parent.parent / "docs"
    site = DocsSite(root)
    assert set(site.pages) == {"gateway", "contract"}
    assert site.openapi_path.is_file()


@pytest.mark.parametrize(
    "manifest",
    [
        b"not json",
        b"[]",
        b'{"documents": "x"}',
        b'{"documents": [{"slug": "Bad Slug", "path": "/docs/a.md"}]}',
        b'{"documents": [{"slug": "a", "path": "/etc/passwd"}]}',
        b'{"documents": [{"slug": "a", "path": "/docs/../x.md"}]}',
        b'{"documents": [{"slug": "a", "path": "/docs/a.md"}, '
        b'{"slug": "a", "path": "/docs/b.md"}]}',
    ],
)
def test_invalid_manifests_are_rejected(manifest: bytes) -> None:
    with pytest.raises(InvalidDocs):
        parse_manifest(manifest)


def test_manifest_metadata_is_optional_and_titles_default() -> None:
    metadata, entries = parse_manifest(b'{"documents": [{"slug": "readme", "path": "/docs/R.md"}]}')
    assert metadata == {
        "version": None,
        "revision": None,
        "contract_version": None,
        "openapi": None,
    }
    assert entries == [("readme", "readme", "/docs/R.md")]


async def test_zip_page_lists_services_and_carries_a_csrf_token(service: FakeUpstream) -> None:
    async with client_for(f"claude={service.url},grok=http://127.0.0.1:1") as client:
        await sign_in(client)
        page = await client.get("/docs/zip")
        script = await client.get("/docs/static/zip.js")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert 'data-csrf="' in page.text
    assert 'value="claude" checked>' in page.text  # the first ready service is preselected
    assert 'value="grok" disabled>' in page.text  # unreachable services cannot be chosen
    assert 'href="/docs/zip"' in page.text  # in the header and the nav
    assert script.headers["content-type"].startswith("text/javascript")
    assert "/docs/zip/jobs" in script.text


async def test_my_api_keys_create_use_and_revoke(service: FakeUpstream) -> None:
    import re

    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        page = await client.get("/docs/keys")
        assert page.status_code == 200
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf is not None
        same_origin = {"Origin": "http://gw"}

        refused = await client.post("/docs/keys", data={"name": "laptop"}, headers=same_origin)
        assert refused.status_code == 403  # no CSRF token
        cross_site = await client.post(
            "/docs/keys",
            data={"name": "laptop", "csrf": csrf.group(1)},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_site.status_code == 403

        created = await client.post(
            "/docs/keys", data={"name": "laptop", "csrf": csrf.group(1)}, headers=same_origin
        )
        assert created.status_code == 200
        key = re.search(r"(zk_[a-z0-9]{8}_[A-Za-z0-9_-]{43})", created.text)
        assert key is not None
        prefix = key.group(1)[3:11]
        listing = (await client.get("/docs/keys")).text
        assert f"zk_{prefix}_..." in listing
        assert key.group(1) not in listing  # shown once, never again

        bearer = {"Authorization": f"Bearer {key.group(1)}"}
        assert (await client.get("/claude/health/ready", headers=bearer)).status_code == 200

        revoked = await client.post(
            f"/docs/keys/{prefix}/revoke", data={"csrf": csrf.group(1)}, headers=same_origin
        )
        assert revoked.status_code == 303
        assert (await client.get("/claude/health/ready", headers=bearer)).status_code == 401
        again = await client.post(
            f"/docs/keys/{prefix}/revoke", data={"csrf": csrf.group(1)}, headers=same_origin
        )
        assert again.status_code == 404


async def test_invalid_key_names_are_explained(service: FakeUpstream) -> None:
    import re

    async with client_for(f"claude={service.url}") as client:
        await sign_in(client)
        csrf = re.search(r'name="csrf" value="([^"]+)"', (await client.get("/docs/keys")).text)
        assert csrf is not None
        response = await client.post(
            "/docs/keys",
            data={"name": "<script>", "csrf": csrf.group(1)},
            headers={"Origin": "http://gw"},
        )
    assert response.status_code == 422
    assert "Use 1-64 letters" in response.text
    assert "<script>" not in response.text.split("<main>")[1].split("</form>")[0]
