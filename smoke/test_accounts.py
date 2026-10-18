"""The account flow end to end, locally: invite codes, sign-in, user keys and the admin
pages, through the real gateway and fake-cognito (the local stand-in for Amazon Cognito).

Skipped unless FAKE_COGNITO_URL is set (docker compose sets it; the live smoke tests
against AWS do not, as real Cognito would need real mailboxes).

A browser would use the public URLs (http://localhost:<port>); inside compose they are
reached by service name. Each test browser maps one to the other, and keeps its cookies
per host, as a browser does.
"""

import html
import os
import re
import uuid

import httpx
import pytest

FAKE_COGNITO_URL = os.environ.get("FAKE_COGNITO_URL", "").rstrip("/")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000").rstrip("/")
PUBLIC_GATEWAY_URL = os.environ.get("PUBLIC_GATEWAY_URL", "http://localhost:8080").rstrip("/")
PUBLIC_COGNITO_URL = os.environ.get("PUBLIC_COGNITO_URL", "http://localhost:9229").rstrip("/")
POOL_ID = os.environ.get("FAKE_COGNITO_POOL_ID", "eu-west-2_LocalDev1")
ORIGIN = {"Origin": GATEWAY_URL}
GUEST_PASSWORD = "Guest-Password-1"

pytestmark = pytest.mark.skipif(not FAKE_COGNITO_URL, reason="no fake-cognito here")


class Browser:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=30)

    @staticmethod
    def internal(url: str) -> str:
        for public, inside in ((PUBLIC_GATEWAY_URL, GATEWAY_URL), (PUBLIC_COGNITO_URL, FAKE_COGNITO_URL)):
            if url.startswith(public):
                return inside + url[len(public):]
        return url

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.client.get(self.internal(url), **kwargs)  # type: ignore[arg-type]

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self.client.post(self.internal(url), **kwargs)  # type: ignore[arg-type]

    def follow(self, response: httpx.Response, base: str) -> httpx.Response:
        while response.status_code in (302, 303):
            location = response.headers["location"]
            response = self.get(location if "://" in location else base + location)
            base = str(response.request.url.copy_with(path="/", query=None)).rstrip("/")
        return response

    def sign_in(self, email: str, password: str, new_password: str) -> httpx.Response:
        """Gateway sign-in, through fake-cognito's hosted page; returns the landing page."""
        response = self.get(f"{GATEWAY_URL}/account/auth/start", params={"next": "/"})
        assert response.status_code == 303
        form = self.get(response.headers["location"])
        assert form.status_code == 200, form.text
        fields = dict(re.findall(r"name='([^']+)' value='([^']*)'", form.text))
        fields = {k: html.unescape(v) for k, v in fields.items()}
        page = self.post(f"{FAKE_COGNITO_URL}/login",
                         data={**fields, "username": email, "password": password})
        if "Change your password" in page.text:
            challenge = re.search(r"name='challenge' value='([^']+)'", page.text)
            assert challenge
            page = self.post(f"{FAKE_COGNITO_URL}/login/new-password", data={
                "challenge": html.unescape(challenge.group(1)),
                "password": new_password, "confirm": new_password})
        assert page.status_code == 302, page.text
        return self.follow(page, FAKE_COGNITO_URL)


def admin_api(operation: str, body: dict[str, object]) -> httpx.Response:
    """fake-cognito's admin API, in the AWS JSON protocol boto3 and the AWS CLI use."""
    return httpx.post(
        f"{FAKE_COGNITO_URL}/",
        json={"UserPoolId": POOL_ID, **body},
        headers={"X-Amz-Target": f"AWSCognitoIdentityProviderService.{operation}",
                 "Content-Type": "application/x-amz-json-1.1"},
    )


def temporary_password(email: str) -> str:
    mails = httpx.get(f"{FAKE_COGNITO_URL}/outbox.json").json()
    return str(next(m["temporary_password"] for m in mails if m["to"] == email))


def csrf(page: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', page)
    assert found, "no CSRF token on the page"
    return found.group(1)


def test_invite_sign_in_key_and_admin_pages() -> None:
    run = uuid.uuid4().hex[:8]
    admin_email, guest_email = f"admin-{run}@example.com", f"guest-{run}@example.com"

    # An admin, created the way `make dev-admin` (and the runbook's commands on AWS) does it.
    created = admin_api("AdminCreateUser", {
        "Username": admin_email,
        "UserAttributes": [{"Name": "email", "Value": admin_email},
                           {"Name": "email_verified", "Value": "true"}]})
    assert created.status_code == 200, created.text
    assert admin_api("AdminAddUserToGroup", {"Username": admin_email, "GroupName": "admin"}).status_code == 200

    admin = Browser()
    home = admin.sign_in(admin_email, temporary_password(admin_email), "Admin-Password-1")
    assert home.status_code == 200
    assert admin_email in home.text
    invites = admin.get(f"{GATEWAY_URL}/account/admin/invites")
    assert invites.status_code == 200

    # An invite code, for someone whose email the admin doesn't know: they choose their
    # own email and password on /account/join. Nothing is emailed.
    made = admin.post(f"{GATEWAY_URL}/account/admin/invites", headers=ORIGIN,
                      data={"csrf": csrf(invites.text), "label": f"smoke {run}", "days": "1"})
    code = re.search(r'class="invite-code">([A-Z0-9-]+)<', made.text)
    assert code, made.text
    assert f"{PUBLIC_GATEWAY_URL}/account/join" in made.text
    guest = Browser()
    assert guest.get(f"{GATEWAY_URL}/account/join").status_code == 200
    joined = guest.post(f"{GATEWAY_URL}/account/join", headers=ORIGIN, data={
        "code": code.group(1), "email": guest_email,
        "password": GUEST_PASSWORD, "confirm": GUEST_PASSWORD})
    assert joined.status_code == 200, joined.text
    assert "Your account is ready" in joined.text
    reused = guest.post(f"{GATEWAY_URL}/account/join", headers=ORIGIN, data={
        "code": code.group(1), "email": f"other-{run}@example.com",
        "password": GUEST_PASSWORD, "confirm": GUEST_PASSWORD})
    assert reused.status_code == 410  # used up

    # The guest signs in with the password they chose, makes a key, and zips with it.
    landing = guest.sign_in(guest_email, GUEST_PASSWORD, "unused: no change step")
    assert guest_email in landing.text
    assert guest.get(f"{GATEWAY_URL}/account/admin/invites").status_code == 403
    keys = guest.get(f"{GATEWAY_URL}/account/keys")
    assert "daily allowance" in keys.text
    made_key = guest.post(f"{GATEWAY_URL}/account/keys", headers=ORIGIN,
                          data={"csrf": csrf(keys.text), "name": "smoke"})
    key = re.search(r"zk_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}", made_key.text)
    assert key, made_key.text
    zipped = httpx.post(f"{GATEWAY_URL}/claude/api/v1/zip", timeout=60,
                        headers={"Authorization": f"Bearer {key.group(0)}"},
                        files={"files": ("hello.txt", b"hello from a user key\n")})
    assert zipped.status_code == 200
    assert zipped.content[:2] == b"PK"

    # The admin sees who used the invite.
    users = admin.get(f"{GATEWAY_URL}/account/admin/users")
    assert guest_email in users.text
    assert f"smoke {run}" in users.text
