"""Cognito sign-in, invite links and the admin pages, end to end in-process.

Cognito is faked at the HTTP level (token endpoint and JWKS, with a test RSA key), so the
real OIDC code runs, PKCE and ID-token checks included. The directory (AdminCreateUser)
is a fake; the database is SQLite in memory.
"""

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.accounts.directory import DirectoryError, UserExists
from gateway.accounts.invites import InviteService, Redeemed, token_hash
from gateway.accounts.keys import KeyService, utc_now
from gateway.accounts.oidc import CognitoOidc, FlowCookie
from gateway.accounts.quota import QuotaService
from gateway.accounts.service import Accounts
from gateway.accounts.store import SqlStore
from gateway.accounts.users import UserGate
from gateway.app import Gateway
from gateway.config import Settings
from gateway.docs import DocsSite
from tests.support import API_KEY, TEST_DATABASE_URL, close_store, make_settings

pytestmark = pytest.mark.anyio

DOMAIN = "https://login.example.com"
POOL = "eu-west-2_Test123"
CLIENT_ID = "client-123"
ISSUER = f"https://cognito-idp.eu-west-2.amazonaws.com/{POOL}"
ORIGIN = {"Origin": "http://gw"}

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


class FakeCognito:
    """Cognito's token endpoint and JWKS. Issues an ID token for the pending sign-in."""

    def __init__(self) -> None:
        self.nonce = ""
        self.challenge = ""
        self.claims: dict[str, Any] = {}
        self.overrides: dict[str, Any] = {}
        self.sign_with = SIGNING_KEY

    def expect(self, authorize_url: str) -> str:
        """Note the nonce and PKCE challenge of a sign-in; return its state."""
        assert authorize_url.startswith(f"{DOMAIN}/oauth2/authorize?")
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == "http://gw/docs/auth/callback"
        self.nonce, self.challenge = query["nonce"], query["code_challenge"]
        return query["state"]

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "token_use": "id",
            "nonce": self.nonce,
            **self.claims,
            **self.overrides,
        }
        return jwt.encode(claims, self.sign_with, algorithm="RS256", headers={"kid": "k1"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{POOL}/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [_jwk(SIGNING_KEY, "k1")]})
        if str(request.url) == f"{DOMAIN}/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            verifier = hashlib.sha256(form["code_verifier"].encode()).digest()
            pkce_ok = base64.urlsafe_b64encode(verifier).rstrip(b"=").decode() == self.challenge
            if form["code"] != "good-code" or not pkce_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token()})
        return httpx.Response(404)


class FakeDirectory:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.disabled: list[str] = []
        self.fail: Exception | None = None

    async def create_user(self, email: str, invite_id: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.created.append((email, invite_id))

    async def disable_user(self, sub: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.disabled.append(sub)


def cognito_settings(**env: str) -> Settings:
    return make_settings(
        GATEWAY_COGNITO_DOMAIN=DOMAIN,
        GATEWAY_COGNITO_CLIENT_ID=CLIENT_ID,
        GATEWAY_COGNITO_CLIENT_SECRET="client-secret",
        GATEWAY_COGNITO_USER_POOL_ID=POOL,
        GATEWAY_PUBLIC_URL="http://gw",
        **env,
    )


class Site:
    """A gateway with fake Cognito, and helpers to act as a browser against it."""

    def __init__(self, **env: str) -> None:
        self.settings = cognito_settings(**env)
        assert self.settings.cognito is not None
        self.cognito = FakeCognito()
        self.directory = FakeDirectory()
        self.store = SqlStore.from_url(TEST_DATABASE_URL)
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.cognito.handle))
        self.accounts = Accounts(
            store=self.store,
            keys=KeyService(self.store),
            invites=InviteService(self.store, self.directory),
            users=UserGate(self.store),
            quota=QuotaService(self.store, self.settings.user_daily_quota_bytes),
            flow=FlowCookie(self.settings.session_secret),
            oidc=CognitoOidc(self.settings.cognito, http=http),
            directory=self.directory,
        )
        self.app = Gateway(self.settings, DocsSite(), self.accounts)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gw")

    async def start(self, client: httpx.AsyncClient, next_path: str = "/docs") -> str:
        response = await client.get("/docs/auth/start", params={"next": next_path})
        assert response.status_code == 303
        assert "path=/docs/auth" in response.headers["set-cookie"].lower()
        return self.cognito.expect(response.headers["location"])

    async def sign_in(
        self,
        client: httpx.AsyncClient,
        sub: str = "sub-ada",
        email: str = "Ada@Example.com",
        *,
        admin: bool = False,
        invite_id: str | None = None,
        next_path: str = "/docs",
    ) -> httpx.Response:
        state = await self.start(client, next_path)
        self.cognito.claims = {"sub": sub, "email": email}
        if admin:
            self.cognito.claims["cognito:groups"] = ["admin"]
        if invite_id:
            self.cognito.claims["custom:invite_id"] = invite_id
        return await client.get("/docs/auth/callback", params={"code": "good-code", "state": state})


def csrf_of(page: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', page)
    assert found is not None
    return found.group(1)


@pytest.fixture
async def site() -> AsyncIterator[Site]:
    site = Site()
    yield site
    await close_store(site.store)


# ---------------------------------------------------------------------- signing in


async def test_sign_in_with_cognito(site: Site) -> None:
    async with site.browser() as client:
        response = await site.sign_in(client, next_path="/docs/keys")
        assert response.status_code == 303
        assert response.headers["location"] == "/docs/keys"
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("gw_docs_session="))
        assert "samesite=lax" in session_cookie.lower()
        assert "httponly" in session_cookie.lower()
        home = await client.get("/docs")
        assert home.status_code == 200
        assert "ada@example.com" in home.text  # email lower-cased
        assert "/docs/admin/invites" not in home.text  # not an admin
        assert (await client.get("/docs/admin/invites")).status_code == 403
    user = await site.store.get_user("sub-ada")
    assert user is not None
    assert user.login_count == 1
    assert user.last_login is not None
    assert len(await site.store.recent_logins("sub-ada")) == 1


async def test_a_second_sign_in_counts(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client)
        await site.sign_in(client)
    user = await site.store.get_user("sub-ada")
    assert user is not None
    assert user.login_count == 2


async def test_login_page_offers_cognito_and_key_sign_in(site: Site) -> None:
    async with site.browser() as client:
        page = await client.get("/docs/login")
    assert 'href="/docs/auth/start?next=/docs"' in page.text
    assert 'name="api_key"' in page.text


async def test_key_sign_in_can_be_turned_off() -> None:
    site = Site(GATEWAY_ALLOW_KEY_LOGIN="false")
    try:
        async with site.browser() as client:
            assert 'name="api_key"' not in (await client.get("/docs/login")).text
            response = await client.post("/docs/login", data={"api_key": API_KEY})
            assert response.status_code == 404
    finally:
        await close_store(site.store)


async def test_callback_needs_the_flow_cookie_and_state(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        site.cognito.claims = {"sub": "sub-ada", "email": "ada@example.com"}
        wrong = await client.get("/docs/auth/callback", params={"code": "good-code", "state": "x"})
        assert wrong.status_code == 400
        assert "gw_docs_session" not in wrong.headers.get("set-cookie", "")
    async with site.browser() as other:  # no flow cookie: a forged callback link
        forged = await other.get(
            "/docs/auth/callback", params={"code": "good-code", "state": state}
        )
        assert forged.status_code == 400


async def test_callback_when_the_user_cancelled(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        response = await client.get(
            "/docs/auth/callback", params={"error": "access_denied", "state": state}
        )
    assert response.status_code == 400
    assert "not completed" in response.text


async def test_bad_code_is_refused(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        site.cognito.claims = {"sub": "sub-ada", "email": "ada@example.com"}
        response = await client.get("/docs/auth/callback", params={"code": "bad", "state": state})
    assert response.status_code == 400
    assert await site.store.get_user("sub-ada") is None


@pytest.mark.parametrize(
    "fault",
    [
        {"aud": "someone-else"},
        {"iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_Other"},
        {"exp": 1000},
        {"token_use": "access"},
        {"nonce": "replayed"},
        {"email": ""},
        "wrong-key",
    ],
)
async def test_id_token_checks(site: Site, fault: dict[str, Any] | str) -> None:
    if fault == "wrong-key":
        site.cognito.sign_with = OTHER_KEY
    else:
        assert isinstance(fault, dict)
        site.cognito.overrides = fault
    async with site.browser() as client:
        response = await site.sign_in(client)
        assert response.status_code == 400
        assert (await client.get("/docs")).status_code == 303  # still signed out
    assert await site.store.get_user("sub-ada") is None


async def test_next_cannot_leave_the_portal(site: Site) -> None:
    async with site.browser() as client:
        response = await site.sign_in(client, next_path="https://evil.example/docs")
    assert response.headers["location"] == "/docs"


async def test_sign_out_also_ends_the_cognito_session(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client)
        response = await client.post("/docs/logout")
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"{DOMAIN}/logout?client_id={CLIENT_ID}")
        assert (await client.get("/docs")).status_code == 303


async def test_operator_sign_out_stays_local(site: Site) -> None:
    async with site.browser() as client:
        await client.post("/docs/login", data={"api_key": API_KEY, "next": "/docs"})
        response = await client.post("/docs/logout")
    assert response.headers["location"] == "/docs/login"


async def test_a_disabled_user_is_signed_out_and_cannot_sign_in(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client)
        assert (await client.get("/docs")).status_code == 200
        await site.store.disable_user("sub-ada", utc_now())
        site.accounts.users.forget("sub-ada")  # as the admin page does
        assert (await client.get("/docs")).status_code == 303
        again = await site.sign_in(client)
        assert again.status_code == 403
        assert "disabled" in again.text


async def test_user_api_keys_belong_to_the_user(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client)
        page = await client.get("/docs/keys")
        created = await client.post(
            "/docs/keys", data={"csrf": csrf_of(page.text), "name": "laptop"}, headers=ORIGIN
        )
        assert created.status_code == 200
    keys = await site.store.list_keys("user:sub-ada")
    assert [key.owner_label for key in keys] == ["ada@example.com"]


# ---------------------------------------------------------------------- invites


async def create_invite(site: Site, admin: httpx.AsyncClient, label: str = "Recruiter") -> str:
    page = await admin.get("/docs/admin/invites")
    assert page.status_code == 200
    response = await admin.post(
        "/docs/admin/invites",
        data={"csrf": csrf_of(page.text), "label": label, "days": "7"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    found = re.search(r"http://gw/docs/invite/([A-Za-z0-9_-]+)", response.text)
    assert found is not None
    return found.group(1)


async def test_invite_link_creates_one_account(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        invites = await site.store.list_invites()
        assert len(invites) == 1
        assert token not in json.dumps([vars(i) for i in invites], default=str)  # hash only

        page = await guest.get(f"/docs/invite/{token}")
        assert page.status_code == 200
        assert 'name="email"' in page.text
        done = await guest.post(
            f"/docs/invite/{token}", data={"email": " Grace@Example.com "}, headers=ORIGIN
        )
        assert done.status_code == 200
        assert "Check your email" in done.text
        assert site.directory.created == [("grace@example.com", invites[0].id)]

        assert (await guest.get(f"/docs/invite/{token}")).status_code == 410  # used up
        again = await guest.post(
            f"/docs/invite/{token}", data={"email": "eve@example.com"}, headers=ORIGIN
        )
        assert again.status_code == 410
        assert len(site.directory.created) == 1

        # Their first sign-in links the account to the invite on the admin pages.
        await site.sign_in(guest, "sub-grace", "grace@example.com", invite_id=invites[0].id)
        users = await admin.get("/docs/admin/users")
        assert "grace@example.com" in users.text
        assert "Recruiter" in users.text
        listing = await admin.get("/docs/admin/invites")
        assert 'class="ok">used<' in listing.text
    used = (await site.store.list_invites())[0]
    assert used.used_by == "sub-grace"
    assert used.email == "grace@example.com"


async def test_invite_needs_same_origin(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        for headers in ({}, {"Origin": "https://evil.example"}):
            response = await guest.post(
                f"/docs/invite/{token}", data={"email": "a@example.com"}, headers=headers
            )
            assert response.status_code == 403
    assert site.directory.created == []


async def test_invite_rejects_a_bad_email(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        response = await guest.post(
            f"/docs/invite/{token}", data={"email": "not-an-email"}, headers=ORIGIN
        )
        assert response.status_code == 422
        assert (await guest.get(f"/docs/invite/{token}")).status_code == 200


async def test_invite_posts_are_rate_limited(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        for _ in range(10):
            bad = await guest.post(f"/docs/invite/{token}", data={"email": "x"}, headers=ORIGIN)
            assert bad.status_code == 422
        limited = await guest.post(
            f"/docs/invite/{token}", data={"email": "a@example.com"}, headers=ORIGIN
        )
        assert limited.status_code == 429
        assert site.directory.created == []  # Cognito was never called
        assert (await guest.get(f"/docs/invite/{token}")).status_code == 200  # still usable


@pytest.mark.parametrize(
    ("failure", "status"), [(UserExists("x"), 409), (DirectoryError("Throttled"), 503)]
)
async def test_invite_survives_a_failed_account_creation(
    site: Site, failure: Exception, status: int
) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        site.directory.fail = failure
        response = await guest.post(
            f"/docs/invite/{token}", data={"email": "a@example.com"}, headers=ORIGIN
        )
        assert response.status_code == status
        assert (await guest.get(f"/docs/invite/{token}")).status_code == 200  # still works


async def test_unknown_and_revoked_invites(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        assert (await guest.get("/docs/invite/nope")).status_code == 410
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        token = await create_invite(site, admin)
        invite_id = (await site.store.list_invites())[0].id
        page = await admin.get("/docs/admin/invites")
        revoked = await admin.post(
            f"/docs/admin/invites/{invite_id}/revoke",
            data={"csrf": csrf_of(page.text)},
            headers=ORIGIN,
        )
        assert revoked.status_code == 303
        assert (await guest.get(f"/docs/invite/{token}")).status_code == 410


async def test_an_invite_works_once_under_concurrency(site: Site) -> None:
    created = await site.accounts.invites.create("Race", 1, "admin")
    outcomes = await asyncio.gather(
        *(site.accounts.invites.redeem(created.token, f"u{i}@example.com") for i in range(5))
    )
    assert outcomes.count(Redeemed.CREATED) == 1
    assert outcomes.count(Redeemed.INVALID) == 4
    assert len(site.directory.created) == 1


async def test_an_expired_invite_cannot_be_used(site: Site) -> None:
    created = await site.accounts.invites.create("Old", 1, "admin")
    later = utc_now().replace(year=utc_now().year + 1)
    assert await site.store.claim_invite(token_hash(created.token), "a@example.com", later) is None


async def test_invite_label_and_days_are_checked(site: Site) -> None:
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/docs/admin/invites")
        for label, days in (("<script>", "7"), ("Recruiter", "0"), ("Recruiter", "x")):
            response = await admin.post(
                "/docs/admin/invites",
                data={"csrf": csrf_of(page.text), "label": label, "days": days},
                headers=ORIGIN,
            )
            assert response.status_code == 422
    assert await site.store.list_invites() == []


# ---------------------------------------------------------------------- admin pages


async def test_admin_pages_need_the_admin_group(site: Site) -> None:
    async with site.browser() as client:
        assert (await client.get("/docs/admin/users")).status_code == 303  # signed out
        await client.post("/docs/login", data={"api_key": API_KEY, "next": "/docs"})
        for page in ("invites", "users", "keys"):
            assert (await client.get(f"/docs/admin/{page}")).status_code == 403  # operator
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        home = await admin.get("/docs")
        assert "/docs/admin/invites" in home.text
        for page in ("invites", "users", "keys"):
            assert (await admin.get(f"/docs/admin/{page}")).status_code == 200


async def test_admin_actions_need_csrf_and_origin(site: Site) -> None:
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/docs/admin/invites")
        no_csrf = await admin.post(
            "/docs/admin/invites", data={"label": "x", "days": "7"}, headers=ORIGIN
        )
        no_origin = await admin.post(
            "/docs/admin/invites", data={"csrf": csrf_of(page.text), "label": "x", "days": "7"}
        )
    assert no_csrf.status_code == 403
    assert no_origin.status_code == 403
    assert await site.store.list_invites() == []


async def test_admin_disables_a_user(site: Site) -> None:
    async with site.browser() as admin, site.browser() as user:
        await site.sign_in(user, "sub-grace", "grace@example.com")
        keys_page = await user.get("/docs/keys")
        await user.post(
            "/docs/keys", data={"csrf": csrf_of(keys_page.text), "name": "ci"}, headers=ORIGIN
        )
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/docs/admin/users")
        csrf = csrf_of(page.text)

        myself = await admin.post(
            "/docs/admin/users/sub-admin/disable", data={"csrf": csrf}, headers=ORIGIN
        )
        assert myself.status_code == 409
        missing = await admin.post(
            "/docs/admin/users/nobody/disable", data={"csrf": csrf}, headers=ORIGIN
        )
        assert missing.status_code == 404

        done = await admin.post(
            "/docs/admin/users/sub-grace/disable", data={"csrf": csrf}, headers=ORIGIN
        )
        assert done.status_code == 303
        assert site.directory.disabled == ["sub-grace"]
        assert (await user.get("/docs")).status_code == 303  # signed out at once
    grace = await site.store.get_user("sub-grace")
    assert grace is not None
    assert not grace.active
    assert not any(key.active for key in await site.store.list_keys("user:sub-grace"))


async def test_disable_changes_nothing_if_cognito_fails(site: Site) -> None:
    async with site.browser() as admin, site.browser() as user:
        await site.sign_in(user, "sub-grace", "grace@example.com")
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/docs/admin/users")
        site.directory.fail = DirectoryError("Throttled")
        response = await admin.post(
            "/docs/admin/users/sub-grace/disable", data={"csrf": csrf_of(page.text)}, headers=ORIGIN
        )
        assert response.status_code == 502
        assert (await user.get("/docs")).status_code == 200
    grace = await site.store.get_user("sub-grace")
    assert grace is not None
    assert grace.active


async def test_admin_sees_and_revokes_any_key(site: Site) -> None:
    async with site.browser() as admin, site.browser() as user:
        await site.sign_in(user, "sub-grace", "grace@example.com")
        keys_page = await user.get("/docs/keys")
        await user.post(
            "/docs/keys", data={"csrf": csrf_of(keys_page.text), "name": "ci"}, headers=ORIGIN
        )
        [key] = await site.store.list_keys("user:sub-grace")
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        listing = await admin.get("/docs/admin/keys")
        assert "grace@example.com" in listing.text
        assert f"zk_{key.prefix}_" in listing.text
        revoked = await admin.post(
            f"/docs/admin/keys/{key.prefix}/revoke",
            data={"csrf": csrf_of(listing.text)},
            headers=ORIGIN,
        )
        assert revoked.status_code == 303
    [after] = await site.store.list_keys("user:sub-grace")
    assert not after.active


async def test_without_cognito_there_is_no_sign_up() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Gateway(make_settings(), DocsSite())),
        base_url="http://gw",
    ) as client:
        assert (await client.get("/docs/auth/start")).status_code == 404
        assert (await client.get("/docs/invite/anything")).status_code == 404
        assert "/docs/auth/start" not in (await client.get("/docs/login")).text
