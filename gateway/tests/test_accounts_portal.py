"""Cognito sign-in, invite codes and the admin pages, end to end in-process.

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

from gateway.accounts.directory import (
    DirectoryError,
    DirectoryUser,
    InvalidPassword,
    UserExists,
)
from gateway.accounts.invites import (
    InviteService,
    Redeemed,
    new_code,
    normalise_code,
    token_hash,
)
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
PASSWORD = "Grace-Password-1"

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
        self.calls: list[str] = []

    def expect(self, authorize_url: str) -> str:
        """Note the nonce and PKCE challenge of a sign-in; return its state."""
        assert urlsplit(authorize_url).path == "/oauth2/authorize"
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == "http://gw/account/auth/callback"
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
        self.calls.append(f"{request.url.scheme}://{request.url.netloc.decode()}{request.url.path}")
        if request.url.path == f"/{POOL}/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [_jwk(SIGNING_KEY, "k1")]})
        if request.url.path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            verifier = hashlib.sha256(form["code_verifier"].encode()).digest()
            pkce_ok = base64.urlsafe_b64encode(verifier).rstrip(b"=").decode() == self.challenge
            if form["code"] != "good-code" or not pkce_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token()})
        return httpx.Response(404)


class FakeDirectory:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.disabled: list[str] = []
        self.fail: Exception | None = None
        self.admins: set[str] = set()  # members of the "admin" group
        self.deleted: set[str] = set()  # deleted in the Cognito console
        self.state_fails = False
        self.lookups: list[tuple[str, bool]] = []  # (sub, whether groups were asked for)

    async def create_user(self, email: str, invite_id: str, password: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.created.append((email, invite_id, password))

    async def disable_user(self, sub: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.disabled.append(sub)

    async def user_state(self, sub: str, *, groups: bool) -> DirectoryUser | None:
        """Cognito's view, as users.py asks for it: everyone exists unless deleted."""
        self.lookups.append((sub, groups))
        if self.state_fails:
            raise DirectoryError("Throttled")
        if sub in self.deleted:
            return None
        admin = sub in self.admins if groups else None
        return DirectoryUser(enabled=sub not in self.disabled, admin=admin)


def cognito_settings(**env: str) -> Settings:
    base = {
        "GATEWAY_COGNITO_DOMAIN": DOMAIN,
        "GATEWAY_COGNITO_CLIENT_ID": CLIENT_ID,
        "GATEWAY_COGNITO_CLIENT_SECRET": "client-secret",
        "GATEWAY_COGNITO_USER_POOL_ID": POOL,
        "GATEWAY_PUBLIC_URL": "http://gw",
    }
    return make_settings(**{**base, **env})


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
            users=UserGate(self.store, self.directory),
            quota=QuotaService(self.store, self.settings.user_daily_quota_bytes),
            flow=FlowCookie(self.settings.session_secret),
            oidc=CognitoOidc(self.settings.cognito, http=http),
            directory=self.directory,
        )
        self.app = Gateway(self.settings, DocsSite(), self.accounts)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gw")

    async def start(self, client: httpx.AsyncClient, next_path: str = "/docs") -> str:
        response = await client.get("/account/auth/start", params={"next": next_path})
        assert response.status_code == 303
        assert "path=/account/auth" in response.headers["set-cookie"].lower()
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
            self.directory.admins.add(sub)  # and Cognito agrees when re-checked
        if invite_id:
            self.cognito.claims["custom:invite_id"] = invite_id
        return await client.get(
            "/account/auth/callback", params={"code": "good-code", "state": state}
        )


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
        response = await site.sign_in(client, next_path="/account/keys")
        assert response.status_code == 303
        assert response.headers["location"] == "/account/keys"
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("gw_docs_session="))
        assert "samesite=lax" in session_cookie.lower()
        assert "httponly" in session_cookie.lower()
        home = await client.get("/")
        assert home.status_code == 200
        assert "ada@example.com" in home.text  # email lower-cased
        assert "/account/admin/invites" not in home.text  # not an admin
        assert (await client.get("/account/admin/invites")).status_code == 403
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
        page = await client.get("/account/login")
    assert 'href="/account/auth/start?next=/"' in page.text
    assert 'name="api_key"' in page.text


async def test_key_sign_in_can_be_turned_off() -> None:
    site = Site(GATEWAY_ALLOW_KEY_LOGIN="false")
    try:
        async with site.browser() as client:
            assert 'name="api_key"' not in (await client.get("/account/login")).text
            response = await client.post(
                "/account/login", data={"api_key": API_KEY}, headers=ORIGIN
            )
            assert response.status_code == 404
    finally:
        await close_store(site.store)


async def test_callback_needs_the_flow_cookie_and_state(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        site.cognito.claims = {"sub": "sub-ada", "email": "ada@example.com"}
        wrong = await client.get(
            "/account/auth/callback", params={"code": "good-code", "state": "x"}
        )
        assert wrong.status_code == 400
        assert "gw_docs_session" not in wrong.headers.get("set-cookie", "")
    async with site.browser() as other:  # no flow cookie: a forged callback link
        forged = await other.get(
            "/account/auth/callback", params={"code": "good-code", "state": state}
        )
        assert forged.status_code == 400


async def test_callback_when_the_user_cancelled(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        response = await client.get(
            "/account/auth/callback", params={"error": "access_denied", "state": state}
        )
    assert response.status_code == 400
    assert "not completed" in response.text


async def test_bad_code_is_refused(site: Site) -> None:
    async with site.browser() as client:
        state = await site.start(client)
        site.cognito.claims = {"sub": "sub-ada", "email": "ada@example.com"}
        response = await client.get(
            "/account/auth/callback", params={"code": "bad", "state": state}
        )
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
    assert response.headers["location"] == "/"


async def test_sign_out_also_ends_the_cognito_session(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client)
        response = await client.post("/account/logout", headers=ORIGIN)
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"{DOMAIN}/logout?client_id={CLIENT_ID}")
        assert (await client.get("/docs")).status_code == 303


async def test_operator_sign_out_stays_local(site: Site) -> None:
    async with site.browser() as client:
        await client.post(
            "/account/login", data={"api_key": API_KEY, "next": "/docs"}, headers=ORIGIN
        )
        response = await client.post("/account/logout", headers=ORIGIN)
    assert response.headers["location"] == "/account/login"


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
        page = await client.get("/account/keys")
        created = await client.post(
            "/account/keys", data={"csrf": csrf_of(page.text), "name": "laptop"}, headers=ORIGIN
        )
        assert created.status_code == 200
    keys = await site.store.list_keys("user:sub-ada")
    assert [key.owner_label for key in keys] == ["ada@example.com"]


# ---------------------------------------------------------------------- invites


async def create_invite(site: Site, admin: httpx.AsyncClient, label: str = "Recruiter") -> str:
    """Create a code on the admin page; return it as shown (XXXX-XXXX-XXXX)."""
    page = await admin.get("/account/admin/invites")
    assert page.status_code == 200
    response = await admin.post(
        "/account/admin/invites",
        data={"csrf": csrf_of(page.text), "label": label, "days": "7"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    assert "http://gw/account/join" in response.text
    found = re.search(r'class="invite-code">([A-Z0-9-]+)<', response.text)
    assert found is not None
    return found.group(1)


def join_data(
    code: str, email: str = "grace@example.com", password: str = PASSWORD
) -> dict[str, str]:
    return {"code": code, "email": email, "password": password, "confirm": password}


async def invite_status(site: Site) -> str:
    [invite] = await site.store.list_invites()
    return invite.status


async def test_an_invite_code_creates_one_account(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
        invites = await site.store.list_invites()
        stored = json.dumps([vars(i) for i in invites], default=str)
        assert code not in stored  # only the hash is stored
        assert code.replace("-", "") not in stored

        page = await guest.get("/account/join")
        assert page.status_code == 200
        assert 'name="code"' in page.text
        assert 'name="password"' in page.text
        prefilled = await guest.get("/account/join", params={"code": code})
        assert f'value="{code}"' in prefilled.text
        # Typed loosely: lower case, spaces instead of dashes.
        typed = code.lower().replace("-", " ")
        done = await guest.post(
            "/account/join", data=join_data(typed, " Grace@Example.com "), headers=ORIGIN
        )
        assert done.status_code == 200
        assert "Your account is ready" in done.text
        assert "/account/auth/start" in done.text
        assert site.directory.created == [("grace@example.com", invites[0].id, PASSWORD)]

        again = await guest.post(
            "/account/join", data=join_data(code, "eve@example.com"), headers=ORIGIN
        )
        assert again.status_code == 410  # used up
        assert len(site.directory.created) == 1

        # Their first sign-in links the account to the invite on the admin pages.
        await site.sign_in(guest, "sub-grace", "grace@example.com", invite_id=invites[0].id)
        users = await admin.get("/account/admin/users")
        assert "grace@example.com" in users.text
        assert "Recruiter" in users.text
        listing = await admin.get("/account/admin/invites")
        assert 'class="ok">used<' in listing.text
    used = (await site.store.list_invites())[0]
    assert used.used_by == "sub-grace"
    assert used.email == "grace@example.com"


async def test_join_needs_same_origin(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        for headers in ({}, {"Origin": "https://evil.example"}):
            response = await guest.post("/account/join", data=join_data(code), headers=headers)
            assert response.status_code == 403
    assert site.directory.created == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"email": "not-an-email"}, "valid email"),
        ({"confirm": "Something-Else-1"}, "do not match"),
        ({"password": "short1A", "confirm": "short1A"}, "12 characters"),
        ({"password": "alllowercase123", "confirm": "alllowercase123"}, "upper and lower"),
        ({"password": "No-Digits-Here", "confirm": "No-Digits-Here"}, "number"),
    ],
)
async def test_join_checks_email_and_password_first(
    site: Site, change: dict[str, str], message: str
) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        response = await guest.post(
            "/account/join", data={**join_data(code), **change}, headers=ORIGIN
        )
        assert response.status_code == 422
        assert message in response.text
        assert f'value="{code}"' in response.text  # kept for the next try
        assert PASSWORD not in response.text  # passwords never go back into the page
        assert await invite_status(site) == "unused"
    assert site.directory.created == []


async def test_join_posts_are_rate_limited(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        for _ in range(10):
            wrong = await guest.post(
                "/account/join", data=join_data("AAAA-BBBB-CCCC"), headers=ORIGIN
            )
            assert wrong.status_code == 410
        limited = await guest.post("/account/join", data=join_data(code), headers=ORIGIN)
        assert limited.status_code == 429
        assert site.directory.created == []  # Cognito was never called
        assert await invite_status(site) == "unused"  # still usable


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (UserExists("x"), 409),
        (InvalidPassword("Password does not conform to policy"), 422),
        (DirectoryError("Throttled"), 503),
    ],
)
async def test_the_code_survives_a_failed_account_creation(
    site: Site, failure: Exception, status: int
) -> None:
    async with site.browser() as admin, site.browser() as guest:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        site.directory.fail = failure
        response = await guest.post("/account/join", data=join_data(code), headers=ORIGIN)
        assert response.status_code == status
        assert await invite_status(site) == "unused"
        site.directory.fail = None
        retry = await guest.post("/account/join", data=join_data(code), headers=ORIGIN)
        assert retry.status_code == 200  # the same code works


async def test_unknown_malformed_and_revoked_codes(site: Site) -> None:
    async with site.browser() as admin, site.browser() as guest:
        for wrong in ("AAAA-BBBB-CCCC", "not a code", "0000-1111-OOOO"):
            response = await guest.post("/account/join", data=join_data(wrong), headers=ORIGIN)
            assert response.status_code == 410
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        code = await create_invite(site, admin)
        invite_id = (await site.store.list_invites())[0].id
        page = await admin.get("/account/admin/invites")
        revoked = await admin.post(
            f"/account/admin/invites/{invite_id}/revoke",
            data={"csrf": csrf_of(page.text)},
            headers=ORIGIN,
        )
        assert revoked.status_code == 303
        response = await guest.post("/account/join", data=join_data(code), headers=ORIGIN)
        assert response.status_code == 410
    assert site.directory.created == []


async def test_a_code_works_once_under_concurrency(site: Site) -> None:
    created = await site.accounts.invites.create("Race", 1, "admin")
    outcomes = await asyncio.gather(
        *(
            site.accounts.invites.redeem(created.code, f"u{i}@example.com", PASSWORD)
            for i in range(5)
        )
    )
    assert outcomes.count(Redeemed.CREATED) == 1
    assert outcomes.count(Redeemed.INVALID) == 4
    assert len(site.directory.created) == 1


async def test_an_expired_code_cannot_be_used(site: Site) -> None:
    created = await site.accounts.invites.create("Old", 1, "admin")
    normalised = normalise_code(created.code)
    assert normalised is not None
    later = utc_now().replace(year=utc_now().year + 1)
    assert await site.store.claim_invite(token_hash(normalised), "a@example.com", later) is None


def test_codes_are_typeable_and_varied() -> None:
    codes = {new_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert normalise_code(code) == code.replace("-", "")
        assert not set(code) & set("01OIL")  # nothing to confuse when read aloud
    assert normalise_code(" k7qm 4xht-9cwd ") == "K7QM4XHT9CWD"
    assert normalise_code("K7QM-4XHT") is None


async def test_invite_label_and_days_are_checked(site: Site) -> None:
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/account/admin/invites")
        for label, days in (("<script>", "7"), ("Recruiter", "0"), ("Recruiter", "x")):
            response = await admin.post(
                "/account/admin/invites",
                data={"csrf": csrf_of(page.text), "label": label, "days": days},
                headers=ORIGIN,
            )
            assert response.status_code == 422
    assert await site.store.list_invites() == []


# ---------------------------------------------------------------------- admin pages


async def test_admin_pages_need_the_admin_group(site: Site) -> None:
    async with site.browser() as client:
        assert (await client.get("/account/admin/users")).status_code == 303  # signed out
        await client.post(
            "/account/login", data={"api_key": API_KEY, "next": "/docs"}, headers=ORIGIN
        )
        for page in ("invites", "users", "keys"):
            assert (await client.get(f"/account/admin/{page}")).status_code == 403  # operator
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        home = await admin.get("/docs")
        assert "/account/admin/invites" in home.text
        for page in ("invites", "users", "keys"):
            assert (await admin.get(f"/account/admin/{page}")).status_code == 200


async def test_admin_actions_need_csrf_and_origin(site: Site) -> None:
    async with site.browser() as admin:
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/account/admin/invites")
        no_csrf = await admin.post(
            "/account/admin/invites", data={"label": "x", "days": "7"}, headers=ORIGIN
        )
        no_origin = await admin.post(
            "/account/admin/invites", data={"csrf": csrf_of(page.text), "label": "x", "days": "7"}
        )
    assert no_csrf.status_code == 403
    assert no_origin.status_code == 403
    assert await site.store.list_invites() == []


async def test_admin_disables_a_user(site: Site) -> None:
    async with site.browser() as admin, site.browser() as user:
        await site.sign_in(user, "sub-grace", "grace@example.com")
        keys_page = await user.get("/account/keys")
        await user.post(
            "/account/keys", data={"csrf": csrf_of(keys_page.text), "name": "ci"}, headers=ORIGIN
        )
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        page = await admin.get("/account/admin/users")
        csrf = csrf_of(page.text)

        myself = await admin.post(
            "/account/admin/users/sub-admin/disable", data={"csrf": csrf}, headers=ORIGIN
        )
        assert myself.status_code == 409
        missing = await admin.post(
            "/account/admin/users/nobody/disable", data={"csrf": csrf}, headers=ORIGIN
        )
        assert missing.status_code == 404

        done = await admin.post(
            "/account/admin/users/sub-grace/disable", data={"csrf": csrf}, headers=ORIGIN
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
        page = await admin.get("/account/admin/users")
        site.directory.fail = DirectoryError("Throttled")
        response = await admin.post(
            "/account/admin/users/sub-grace/disable",
            data={"csrf": csrf_of(page.text)},
            headers=ORIGIN,
        )
        assert response.status_code == 502
        assert (await user.get("/docs")).status_code == 200
    grace = await site.store.get_user("sub-grace")
    assert grace is not None
    assert grace.active


async def test_admin_sees_and_revokes_any_key(site: Site) -> None:
    async with site.browser() as admin, site.browser() as user:
        await site.sign_in(user, "sub-grace", "grace@example.com")
        keys_page = await user.get("/account/keys")
        await user.post(
            "/account/keys", data={"csrf": csrf_of(keys_page.text), "name": "ci"}, headers=ORIGIN
        )
        [key] = await site.store.list_keys("user:sub-grace")
        await site.sign_in(admin, "sub-admin", "admin@example.com", admin=True)
        listing = await admin.get("/account/admin/keys")
        assert "grace@example.com" in listing.text
        assert f"zk_{key.prefix}_" in listing.text
        revoked = await admin.post(
            f"/account/admin/keys/{key.prefix}/revoke",
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
        assert (await client.get("/account/auth/start")).status_code == 404
        assert (await client.get("/account/join")).status_code == 404
        assert "/account/auth/start" not in (await client.get("/account/login")).text


async def test_a_back_channel_carries_the_server_side_calls() -> None:
    """Locally the browser reaches fake-cognito at localhost, the gateway by compose name."""
    site = Site(
        GATEWAY_COGNITO_DOMAIN="http://localhost:9229",
        GATEWAY_COGNITO_BACKCHANNEL_URL="http://fake-cognito:9229",
    )
    try:
        async with site.browser() as client:
            start = await client.get("/account/auth/start")
            assert start.headers["location"].startswith("http://localhost:9229/oauth2/authorize?")
            response = await site.sign_in(client)
            assert response.status_code == 303
        assert site.cognito.calls == [
            "http://fake-cognito:9229/oauth2/token",
            f"http://fake-cognito:9229/{POOL}/.well-known/jwks.json",
        ]
    finally:
        await close_store(site.store)
