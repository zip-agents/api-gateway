"""Regressions for the security review (SECURITY.md in the platform repository, kept
local): each test pins one fix. Uses the account tests' fake Cognito and directory."""

import asyncio
import re
from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.accounts.authn import Authenticator
from gateway.accounts.directory import DirectoryUser
from gateway.accounts.keys import KeyService
from gateway.accounts.store import SqlStore
from gateway.app import Gateway
from gateway.auth import KeyRing
from gateway.config import ConfigError
from gateway.docs import DocsSite
from gateway.portal import SCALAR_URL
from tests.support import API_KEY, TEST_DATABASE_URL, close_store, make_settings
from tests.test_accounts_portal import ORIGIN, Site, cognito_settings, csrf_of

pytestmark = pytest.mark.anyio


@pytest.fixture
async def site() -> AsyncIterator[Site]:
    site = Site()
    yield site
    await close_store(site.store)


def a_minute_later(site: Site, sub: str) -> None:
    """The per-user check is cached for a minute; drop the cache as if it had passed."""
    site.accounts.users.forget(sub)


async def make_key(client: httpx.AsyncClient) -> str:
    page = await client.get("/account/keys")
    made = await client.post(
        "/account/keys", data={"csrf": csrf_of(page.text), "name": "k"}, headers=ORIGIN
    )
    found = re.search(r"zk_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}", made.text)
    assert found is not None
    return found.group(0)


async def call_api(client: httpx.AsyncClient, key: str) -> int:
    """401: the key was refused. 502: accepted (the test upstream is unreachable)."""
    response = await client.post("/claude/api/v1/zip", headers={"Authorization": f"Bearer {key}"})
    return response.status_code


# ---------------------------------------------------------------------- 1. keys and owners


async def test_a_key_made_by_a_removed_operator_stops_working() -> None:
    store = SqlStore.from_url(TEST_DATABASE_URL)
    try:
        keys = KeyService(store)
        _, key = await keys.create("op:ops", "ops", "made in an operator session")
        with_ops = Authenticator(KeyRing({"ops": "o" * 40}), keys)
        assert await with_ops.identify(key) is not None
        without_ops = Authenticator(KeyRing({"smoke": API_KEY}), keys)
        assert await without_ops.identify(key) is None
    finally:
        await close_store(store)


async def test_user_keys_stop_when_the_user_is_deleted_in_cognito(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-grace", "grace@example.com")
        key = await make_key(client)
        assert await call_api(client, key) == 502  # accepted
        site.directory.deleted.add("sub-grace")  # deleted in the Cognito console
        a_minute_later(site, "sub-grace")
        assert await call_api(client, key) == 401
        assert (await client.get("/")).status_code == 303  # and signed out of the portal


async def test_user_keys_stop_when_the_user_is_disabled_in_cognito(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-grace", "grace@example.com")
        key = await make_key(client)
        site.directory.disabled.append("sub-grace")  # disabled in the console, not the portal
        a_minute_later(site, "sub-grace")
        assert await call_api(client, key) == 401


# ---------------------------------------------------------------------- 3. live rights


async def test_admin_rights_follow_the_cognito_group(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-admin", "admin@example.com", admin=True)
        assert (await client.get("/account/admin/users")).status_code == 200
        site.directory.admins.discard("sub-admin")  # removed from "admin" in Cognito
        a_minute_later(site, "sub-admin")
        assert (await client.get("/account/admin/users")).status_code == 403
        assert "/account/admin/invites" not in (await client.get("/")).text
        site.directory.admins.add("sub-admin")
        a_minute_later(site, "sub-admin")
        assert (await client.get("/account/admin/users")).status_code == 200


async def test_admin_rights_wait_for_cognito_if_it_cannot_be_reached(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-admin", "admin@example.com", admin=True)
        site.directory.state_fails = True
        a_minute_later(site, "sub-admin")
        assert (await client.get("/")).status_code == 200  # still signed in
        assert (await client.get("/account/admin/users")).status_code == 403  # no admin
        site.directory.state_fails = False
        a_minute_later(site, "sub-admin")
        assert (await client.get("/account/admin/users")).status_code == 200


async def test_signing_out_ends_every_copy_of_the_session(site: Site) -> None:
    async with site.browser() as browser, site.browser() as thief:
        await site.sign_in(browser, "sub-grace", "grace@example.com")
        thief.cookies.set("gw_docs_session", browser.cookies["gw_docs_session"])
        assert (await thief.get("/")).status_code == 200  # a copied cookie works...
        assert (await browser.post("/account/logout", headers=ORIGIN)).status_code == 303
        a_minute_later(site, "sub-grace")
        assert (await thief.get("/")).status_code == 303  # ...until the user signs out
        await site.sign_in(browser, "sub-grace", "grace@example.com")
        assert (await browser.get("/")).status_code == 200  # signing in again works


# ---------------------------------------------------------------------- 6. cross-site posts


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
async def test_sign_in_and_sign_out_refuse_cross_site_posts(
    site: Site, headers: dict[str, str]
) -> None:
    async with site.browser() as client:
        login = await client.post(
            "/account/login", data={"api_key": API_KEY, "next": "/"}, headers=headers
        )
        assert login.status_code == 403
        assert "set-cookie" not in login.headers
        await site.sign_in(client)
        logout = await client.post("/account/logout", headers=headers)
        assert logout.status_code == 403
        assert (await client.get("/")).status_code == 200  # still signed in


async def test_pages_let_browsers_send_their_origin_on_form_posts(site: Site) -> None:
    """With `Referrer-Policy: no-referrer` a browser sends `Origin: null` on every form
    post (Fetch standard, "append a request Origin header"), so the same-origin check
    refused every portal form. `same-origin` still sends nothing to other sites."""
    async with site.browser() as client:
        await site.sign_in(client)
        for path in ("/", "/account/login", "/account/keys", "/zip"):
            page = await client.get(path)
            assert page.headers["referrer-policy"] == "same-origin", path


# ---------------------------------------------------------------------- 5. CSP


async def test_api_reference_allows_only_the_pinned_scalar_scripts() -> None:
    gateway = Gateway(make_settings(), DocsSite())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gw"
    ) as client:
        await client.post("/account/login", data={"api_key": API_KEY, "next": "/"}, headers=ORIGIN)
        page = await client.get("/docs/api")
    csp = page.headers["content-security-policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src")).split()
    assert script_src == [
        "script-src",
        "'self'",
        "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.64.0/",
    ]
    assert SCALAR_URL.startswith(script_src[2])
    assert f'src="{SCALAR_URL}"' in page.text


# ---------------------------------------------------------------------- 8. back channel


def test_the_back_channel_is_refused_unless_cognito_is_the_local_fake() -> None:
    with pytest.raises(ConfigError, match="only for the local fake-cognito"):
        cognito_settings(GATEWAY_COGNITO_BACKCHANNEL_URL="http://fake-cognito:9229")
    local = cognito_settings(
        GATEWAY_COGNITO_DOMAIN="http://localhost:9229",
        GATEWAY_COGNITO_BACKCHANNEL_URL="http://fake-cognito:9229",
    )
    assert local.cognito is not None
    assert local.cognito.backchannel_url == "http://fake-cognito:9229"


# ---------------------------------------------------------------------- the cost of 3


async def test_a_busy_user_costs_one_cognito_lookup_a_minute(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-grace", "grace@example.com")
        key = await make_key(client)
        a_minute_later(site, "sub-grace")
        site.directory.lookups.clear()
        for _ in range(5):
            assert (await client.get("/")).status_code == 200
            assert await call_api(client, key) == 502
        assert site.directory.lookups == [("sub-grace", False)]  # one, and no group call


async def test_only_admin_sessions_ask_cognito_for_groups(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-admin", "admin@example.com", admin=True)
        site.directory.lookups.clear()
        a_minute_later(site, "sub-admin")
        assert (await client.get("/account/admin/users")).status_code == 200
        assert site.directory.lookups == [("sub-admin", True)]


async def test_requests_that_arrive_together_share_one_lookup(site: Site) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-grace", "grace@example.com")
    a_minute_later(site, "sub-grace")
    site.directory.lookups.clear()
    states = await asyncio.gather(*(site.accounts.users.state("sub-grace") for _ in range(5)))
    assert all(state.active for state in states)
    assert site.directory.lookups == [("sub-grace", False)]


async def test_a_lookup_from_before_a_change_is_not_kept(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with site.browser() as client:
        await site.sign_in(client, "sub-grace", "grace@example.com")
    users, directory = site.accounts.users, site.directory
    asked, answer = asyncio.Event(), asyncio.Event()
    real = directory.user_state

    async def slow_user_state(sub: str, *, groups: bool) -> DirectoryUser | None:
        found = await real(sub, groups=groups)  # Cognito's answer before the change...
        asked.set()
        await answer.wait()  # ...arrives after it
        return found

    monkeypatch.setattr(directory, "user_state", slow_user_state)
    a_minute_later(site, "sub-grace")
    before = asyncio.ensure_future(users.state("sub-grace"))
    await asyncio.wait_for(asked.wait(), 5)
    directory.deleted.add("sub-grace")
    users.forget("sub-grace")  # as the portal does after changing a user
    answer.set()
    assert (await before).active  # what it found, but it isn't kept...
    assert not (await users.state("sub-grace")).active  # ...so the next request looks again
