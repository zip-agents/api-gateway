"""The fake's contract, checked with the same client libraries the gateway uses: boto3 for
the admin API (as gateway/accounts/directory.py), httpx and PyJWT for sign-in (as
gateway/accounts/oidc.py). If these pass, the gateway talks to the fake as to Cognito."""

import base64
import hashlib
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

CALLBACK = "http://localhost:8080/account/auth/callback"
os.environ.update(
    FAKE_COGNITO_DEV_ONLY="yes",
    FAKE_COGNITO_STATE=os.path.join(tempfile.mkdtemp(), "state.json"),
    FAKE_COGNITO_CALLBACK_URLS=CALLBACK,
    FAKE_COGNITO_LOGOUT_URLS="http://localhost:8080/account/login",
)

import boto3  # noqa: E402
import httpx  # noqa: E402
import jwt  # noqa: E402
import uvicorn  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import app  # noqa: E402

POOL = app.POOL_ID


@pytest.fixture(scope="session")
def base() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def idp(base: str):  # type: ignore[no-untyped-def]
    return boto3.client(
        "cognito-idp", region_name="eu-west-2", endpoint_url=base,
        aws_access_key_id="local", aws_secret_access_key="local",
    )


def create_user(idp, email: str, invite: str | None = None) -> str:  # type: ignore[no-untyped-def]
    attrs = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
    if invite:
        attrs.append({"Name": "custom:invite_id", "Value": invite})
    idp.admin_create_user(UserPoolId=POOL, Username=email, UserAttributes=attrs,
                          DesiredDeliveryMediums=["EMAIL"])
    return str(httpx.get(f"{base_url(idp)}/outbox.json").json()[0]["temporary_password"])


def base_url(idp) -> str:  # type: ignore[no-untyped-def]
    return str(idp.meta.endpoint_url)


def sign_in(base: str, email: str, password: str, new_password: str | None = None,
            client: httpx.Client | None = None) -> dict[str, str]:
    """The browser half of the flow, then the gateway's token exchange."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": app.CLIENT_ID, "redirect_uri": CALLBACK,
              "scope": "openid email", "state": "s1", "nonce": "n1",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    browser = client or httpx.Client()
    page = browser.get(f"{base}/oauth2/authorize", params=params)
    if page.status_code == 200:  # the sign-in form
        page = browser.post(f"{base}/login", data={**params, "username": email, "password": password})
        if new_password and "Change your password" in page.text:
            found = re.search(r"name='challenge' value='([^']+)'", page.text)
            assert found
            page = browser.post(f"{base}/login/new-password", data={
                "challenge": found.group(1), "password": new_password, "confirm": new_password})
    assert page.status_code == 302, page.text
    query = parse_qs(urlsplit(page.headers["location"]).query)
    assert query["state"] == ["s1"]
    tokens = httpx.post(f"{base}/oauth2/token", auth=(app.CLIENT_ID, app.CLIENT_SECRET), data={
        "grant_type": "authorization_code", "client_id": app.CLIENT_ID, "code": query["code"][0],
        "redirect_uri": CALLBACK, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    return dict(tokens.json())


def verify(base: str, id_token: str) -> dict[str, object]:
    keys = httpx.get(f"{base}/{POOL}/.well-known/jwks.json").json()["keys"]
    kid = jwt.get_unverified_header(id_token)["kid"]
    key = jwt.PyJWK(next(k for k in keys if k["kid"] == kid)).key
    claims: dict[str, object] = jwt.decode(
        id_token, key, algorithms=["RS256"], audience=app.CLIENT_ID,
        issuer=f"https://cognito-idp.eu-west-2.amazonaws.com/{POOL}")
    return claims


def test_admin_create_user_emails_a_temporary_password(idp) -> None:  # type: ignore[no-untyped-def]
    response = idp.admin_create_user(
        UserPoolId=POOL, Username="Grace@Example.com",
        UserAttributes=[{"Name": "email", "Value": "Grace@Example.com"},
                        {"Name": "custom:invite_id", "Value": "inv1"}],
        DesiredDeliveryMediums=["EMAIL"])
    assert response["User"]["UserStatus"] == "FORCE_CHANGE_PASSWORD"
    mail = httpx.get(f"{base_url(idp)}/outbox.json").json()[0]
    assert mail["to"] == "grace@example.com"
    assert mail["temporary_password"] in mail["body"]
    user = idp.admin_get_user(UserPoolId=POOL, Username="grace@example.com")
    assert {"Name": "custom:invite_id", "Value": "inv1"} in user["UserAttributes"]


def test_existing_email_and_unknown_pool_are_modelled_errors(idp) -> None:  # type: ignore[no-untyped-def]
    create_user(idp, "dup@example.com")
    with pytest.raises(idp.exceptions.UsernameExistsException):
        create_user(idp, "dup@example.com")
    with pytest.raises(idp.exceptions.UserNotFoundException):
        idp.admin_get_user(UserPoolId=POOL, Username="nobody@example.com")
    with pytest.raises(ClientError, match="ResourceNotFoundException"):
        idp.admin_get_user(UserPoolId="eu-west-2_Other", Username="dup@example.com")


def test_sign_in_with_a_new_password_and_cognito_claims(base: str, idp) -> None:  # type: ignore[no-untyped-def]
    temporary = create_user(idp, "ada@example.com", invite="inv42")
    idp.admin_add_user_to_group(UserPoolId=POOL, Username="ada@example.com", GroupName="admin")
    tokens = sign_in(base, "ada@example.com", temporary, new_password="Correct-Horse-9")
    claims = verify(base, tokens["id_token"])
    assert claims["token_use"] == "id"
    assert claims["nonce"] == "n1"
    assert claims["email"] == "ada@example.com"
    assert claims["cognito:groups"] == ["admin"]
    assert claims["custom:invite_id"] == "inv42"
    # The temporary password no longer works; the new one does, with no change step.
    browser = httpx.Client()
    params_page = browser.get(f"{base}/oauth2/authorize", params={
        "response_type": "code", "client_id": app.CLIENT_ID, "redirect_uri": CALLBACK,
        "scope": "openid", "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    assert params_page.status_code == 200
    sign_in(base, "ada@example.com", "Correct-Horse-9")


def test_token_endpoint_checks_client_pkce_and_single_use(base: str, idp) -> None:  # type: ignore[no-untyped-def]
    temporary = create_user(idp, "pkce@example.com")
    sign_in(base, "pkce@example.com", temporary, new_password="Another-Pass-7")
    bad = httpx.post(f"{base}/oauth2/token", auth=(app.CLIENT_ID, "wrong"),
                     data={"grant_type": "authorization_code", "code": "x"})
    assert bad.status_code == 401
    unknown = httpx.post(f"{base}/oauth2/token", auth=(app.CLIENT_ID, app.CLIENT_SECRET), data={
        "grant_type": "authorization_code", "code": "nope", "redirect_uri": CALLBACK,
        "code_verifier": "v"})
    assert unknown.json() == {"error": "invalid_grant"}


def test_authorize_rejects_unregistered_redirects(base: str) -> None:
    page = httpx.get(f"{base}/oauth2/authorize", params={
        "response_type": "code", "client_id": app.CLIENT_ID, "redirect_uri": "https://evil.example/cb",
        "scope": "openid", "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    assert page.status_code == 400


def test_disabled_users_cannot_sign_in_and_sign_out_ends_sessions(base: str, idp) -> None:  # type: ignore[no-untyped-def]
    temporary = create_user(idp, "eve@example.com")
    browser = httpx.Client()
    sign_in(base, "eve@example.com", temporary, new_password="Eve-Password-1", client=browser)
    sign_in(base, "eve@example.com", "unused", client=browser)  # the hosted session skips the form
    idp.admin_user_global_sign_out(UserPoolId=POOL, Username="eve@example.com")
    idp.admin_disable_user(UserPoolId=POOL, Username="eve@example.com")
    with pytest.raises(AssertionError):
        sign_in(base, "eve@example.com", "Eve-Password-1", client=browser)


def test_logout_only_to_registered_urls(base: str) -> None:
    ok = httpx.get(f"{base}/logout", params={"client_id": app.CLIENT_ID,
                                             "logout_uri": "http://localhost:8080/account/login"})
    assert ok.status_code == 302
    bad = httpx.get(f"{base}/logout", params={"client_id": app.CLIENT_ID,
                                              "logout_uri": "https://evil.example"})
    assert bad.status_code == 400


def test_join_flow_calls_create_without_email_then_a_permanent_password(base: str, idp) -> None:  # type: ignore[no-untyped-def]
    """What the gateway does on /account/join (gateway/accounts/directory.py)."""
    sent_before = len(httpx.get(f"{base}/outbox.json").json())
    idp.admin_create_user(
        UserPoolId=POOL, Username="joiner@example.com", MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": "joiner@example.com"},
                        {"Name": "email_verified", "Value": "false"},
                        {"Name": "custom:invite_id", "Value": "inv7"}])
    assert len(httpx.get(f"{base}/outbox.json").json()) == sent_before  # nothing emailed
    with pytest.raises(idp.exceptions.InvalidPasswordException):
        idp.admin_set_user_password(UserPoolId=POOL, Username="joiner@example.com",
                                    Password="short", Permanent=True)
    idp.admin_set_user_password(UserPoolId=POOL, Username="joiner@example.com",
                                Password="Joiner-Password-1", Permanent=True)
    user = idp.admin_get_user(UserPoolId=POOL, Username="joiner@example.com")
    assert user["UserStatus"] == "CONFIRMED"
    # Signs straight in: no temporary password, no change step.
    claims = verify(base, sign_in(base, "joiner@example.com", "Joiner-Password-1")["id_token"])
    assert claims["email_verified"] is False
    assert claims["custom:invite_id"] == "inv7"


def test_admin_delete_user_frees_the_email(idp) -> None:  # type: ignore[no-untyped-def]
    create_user(idp, "undo@example.com")
    idp.admin_delete_user(UserPoolId=POOL, Username="undo@example.com")
    with pytest.raises(idp.exceptions.UserNotFoundException):
        idp.admin_get_user(UserPoolId=POOL, Username="undo@example.com")
    create_user(idp, "undo@example.com")  # can be created again


def test_group_membership_and_enabled_state_can_be_read(idp) -> None:  # type: ignore[no-untyped-def]
    """What the gateway checks about once a minute per active user (users.py)."""
    create_user(idp, "member@example.com")
    sub = idp.admin_get_user(UserPoolId=POOL, Username="member@example.com")["Username"]
    assert idp.admin_list_groups_for_user(UserPoolId=POOL, Username=sub)["Groups"] == []
    idp.admin_add_user_to_group(UserPoolId=POOL, Username=sub, GroupName="admin")
    groups = idp.admin_list_groups_for_user(UserPoolId=POOL, Username=sub)["Groups"]
    assert [g["GroupName"] for g in groups] == ["admin"]
    idp.admin_disable_user(UserPoolId=POOL, Username=sub)
    assert idp.admin_get_user(UserPoolId=POOL, Username=sub)["Enabled"] is False
