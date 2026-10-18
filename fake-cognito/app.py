"""A local stand-in for Amazon Cognito, for running the platform with no cloud at all.

It speaks the parts of Cognito's contract the gateway uses, at the same paths and in the
same formats, so the gateway runs its production code (gateway/accounts/oidc.py and
directory.py) against it unchanged:

    GET  /oauth2/authorize           hosted sign-in page (authorization code + PKCE S256)
    POST /oauth2/token               code -> tokens (client secret, PKCE verifier)
    GET  /logout                     ends the hosted-login session
    GET  /<pool>/.well-known/jwks.json  and  /openid-configuration
    POST /                           admin API, AWS JSON 1.1 protocol (X-Amz-Target):
                                     AdminCreateUser, AdminGetUser, AdminSetUserPassword,
                                     AdminDeleteUser, AdminDisableUser, AdminEnableUser,
                                     AdminUserGlobalSignOut, AdminAddUserToGroup,
                                     AdminListGroupsForUser
    GET  /outbox, /outbox.json       the emails Cognito would have sent (temporary passwords)

ID tokens carry Cognito's claims (iss https://cognito-idp.<region>.amazonaws.com/<pool>,
aud, token_use=id, nonce, email, cognito:groups, custom:invite_id), signed RS256.

Not a security boundary and not a full Cognito: no MFA, no forgotten-password flow, no
request signing checks. It refuses to start unless FAKE_COGNITO_DEV_ONLY=yes.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import string
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

if os.environ.get("FAKE_COGNITO_DEV_ONLY") != "yes":
    sys.exit("fake-cognito is for local development only: set FAKE_COGNITO_DEV_ONLY=yes")


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


POOL_ID = os.environ.get("FAKE_COGNITO_POOL_ID", "eu-west-2_LocalDev1")
REGION = POOL_ID.split("_", 1)[0]
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"  # exactly as Cognito's
CLIENT_ID = os.environ.get("FAKE_COGNITO_CLIENT_ID", "local-gateway")
CLIENT_SECRET = os.environ.get("FAKE_COGNITO_CLIENT_SECRET", "local-gateway-secret")
CALLBACK_URLS = _list("FAKE_COGNITO_CALLBACK_URLS")
LOGOUT_URLS = _list("FAKE_COGNITO_LOGOUT_URLS")
LOGIN_URL = os.environ.get("FAKE_COGNITO_LOGIN_URL", "http://localhost:8080/account/login")
STATE_FILE = Path(os.environ.get("FAKE_COGNITO_STATE", "/data/state.json"))
GROUPS = {"admin"}
ID_TOKEN_SECONDS = 300  # as the platform's app client (5 minutes)
CODE_SECONDS = 300
SESSION_COOKIE = "fake_cognito_session"

# ------------------------------------------------------------------------------ state


@dataclass
class User:
    sub: str
    email: str
    created: float
    status: str = "FORCE_CHANGE_PASSWORD"  # then CONFIRMED
    enabled: bool = True
    password: str = ""  # pbkdf2 hash
    groups: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    session_version: int = 0  # bumped by AdminUserGlobalSignOut


class State:
    """Users, the signing key and the outbox, kept in a JSON file (a compose volume), so
    accounts survive restarts as they would in Cognito. Codes and sessions are memory only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        data = json.loads(path.read_text()) if path.exists() else {}
        if "key_pem" in data:
            self.key = serialization.load_pem_private_key(data["key_pem"].encode(), None)
            self.kid = data["kid"]
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.kid = uuid.uuid4().hex
        self.users = {sub: User(**u) for sub, u in data.get("users", {}).items()}
        self.outbox: list[dict[str, Any]] = data.get("outbox", [])
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, tuple[str, int]] = {}
        self.challenges: dict[str, tuple[str, dict[str, str], float]] = {}
        self.save()

    def save(self) -> None:
        pem = self.key.private_bytes(  # type: ignore[union-attr]
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        data = {
            "key_pem": pem,
            "kid": self.kid,
            "users": {sub: asdict(u) for sub, u in self.users.items()},
            "outbox": self.outbox[-200:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def find(self, username: str) -> User | None:
        """By sub or by email: the pool uses the email as its username alias."""
        if username in self.users:
            return self.users[username]
        wanted = username.strip().lower()
        return next((u for u in self.users.values() if u.email == wanted), None)


STATE = State(STATE_FILE)

# ------------------------------------------------------------------------------ helpers


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"{salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt)), stored)


def _password_problem(password: str) -> str | None:
    """The platform pool's policy (terraform/modules/cognito-pool)."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if not (any(c.islower() for c in password) and any(c.isupper() for c in password)):
        return "Password must have upper and lower case letters."
    if not any(c.isdigit() for c in password):
        return "Password must have a number."
    return None


def _temporary_password() -> str:
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits]
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(string.ascii_letters + string.digits) for _ in range(9)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} - fake Cognito</title><style>"
        "body{font:16px/1.5 system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}"
        "label{display:block;margin:.75rem 0}input{width:100%;padding:.5rem;font:inherit}"
        "button{margin-top:1rem;padding:.5rem 1rem;font:inherit}.error{color:#c62828}"
        ".banner{background:#fff3cd;padding:.5rem .75rem;border-radius:6px;font-size:.9rem}"
        "table{border-collapse:collapse}td,th{border-bottom:1px solid #ddd;padding:.3rem .5rem;"
        "text-align:left;vertical-align:top}</style></head><body>"
        "<p class='banner'>Local fake Cognito: development only. "
        "<a href='/outbox'>Outbox</a></p>"
        f"{body}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; "
                 "style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"},
    )


def _hidden(params: dict[str, str]) -> str:
    return "".join(
        f"<input type='hidden' name='{html.escape(k)}' value='{html.escape(v)}'>"
        for k, v in params.items()
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body())[:16384]
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


def _attributes(user: User) -> list[dict[str, str]]:
    attrs = {"sub": user.sub, "email": user.email, "email_verified": "false", **user.attributes}
    return [{"Name": k, "Value": v} for k, v in attrs.items()]


# ------------------------------------------------------------------------------ hosted login

AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
               "code_challenge", "code_challenge_method")


def _auth_params_problem(params: dict[str, str]) -> str | None:
    if params.get("client_id") != CLIENT_ID:
        return "Unknown client_id."
    if params.get("redirect_uri") not in CALLBACK_URLS:
        return "redirect_uri is not one of this app client's callback URLs."
    if params.get("response_type") != "code":
        return "Only response_type=code is supported."
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return "PKCE with code_challenge_method=S256 is required."
    if "openid" not in params.get("scope", "").split():
        return "The openid scope is required."
    return None


def _issue_code(user: User, params: dict[str, str]) -> Response:
    code = secrets.token_urlsafe(24)
    STATE.codes[code] = {
        "sub": user.sub,
        "redirect_uri": params["redirect_uri"],
        "challenge": params["code_challenge"],
        "nonce": params.get("nonce", ""),
        "expires": time.time() + CODE_SECONDS,
    }
    query = {"code": code}
    if params.get("state"):
        query["state"] = params["state"]
    response = RedirectResponse(f"{params['redirect_uri']}?{urlencode(query)}", status_code=302)
    session = secrets.token_urlsafe(24)
    STATE.sessions[session] = (user.sub, user.session_version)
    response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax", path="/")
    return response


def _login_form(params: dict[str, str], error: str = "", status: int = 200) -> HTMLResponse:
    message = f"<p class='error' role='alert'>{html.escape(error)}</p>" if error else ""
    return _page(
        "Sign in",
        f"<h1>Sign in</h1>{message}<form method='post' action='/login'>{_hidden(params)}"
        "<label>Email <input name='username' type='email' autocomplete='username' required "
        "autofocus></label><label>Password <input name='password' type='password' "
        "autocomplete='current-password' required></label><button>Sign in</button></form>",
        status,
    )


def _new_password_form(challenge: str, error: str = "", status: int = 200) -> HTMLResponse:
    message = f"<p class='error' role='alert'>{html.escape(error)}</p>" if error else ""
    return _page(
        "Change password",
        "<h1>Change your password</h1><p>Your temporary password must be replaced.</p>"
        f"{message}<form method='post' action='/login/new-password'>"
        f"<input type='hidden' name='challenge' value='{html.escape(challenge)}'>"
        "<label>New password <input name='password' type='password' "
        "autocomplete='new-password' required autofocus></label>"
        "<label>Confirm <input name='confirm' type='password' autocomplete='new-password' "
        "required></label><p>At least 12 characters, with upper and lower case letters "
        "and a number.</p><button>Change password</button></form>",
        status,
    )


async def authorize(request: Request) -> Response:
    params = {k: request.query_params[k] for k in AUTH_PARAMS if k in request.query_params}
    problem = _auth_params_problem(params)
    if problem:
        return _page("Error", f"<h1>Invalid request</h1><p>{html.escape(problem)}</p>", 400)
    session = STATE.sessions.get(request.cookies.get(SESSION_COOKIE, ""))
    if session is not None:
        user = STATE.users.get(session[0])
        if user and user.enabled and user.status == "CONFIRMED" and user.session_version == session[1]:
            return _issue_code(user, params)  # signed in already: straight back
    return _login_form(params)


async def login(request: Request) -> Response:
    form = await _form(request)
    params = {k: form[k] for k in AUTH_PARAMS if k in form}
    problem = _auth_params_problem(params)
    if problem:
        return _page("Error", f"<h1>Invalid request</h1><p>{html.escape(problem)}</p>", 400)
    user = STATE.find(form.get("username", ""))
    if user is None or not _check_password(form.get("password", ""), user.password):
        return _login_form(params, "Incorrect username or password.", 400)
    if not user.enabled:
        return _login_form(params, "User is disabled.", 400)
    if user.status == "FORCE_CHANGE_PASSWORD":
        challenge = secrets.token_urlsafe(24)
        STATE.challenges[challenge] = (user.sub, params, time.time() + 600)
        return _new_password_form(challenge)
    return _issue_code(user, params)


async def new_password(request: Request) -> Response:
    form = await _form(request)
    challenge = form.get("challenge", "")
    pending = STATE.challenges.get(challenge)
    if pending is None or pending[2] < time.time():
        return _page("Expired", "<h1>That sign-in expired</h1><p>Start again.</p>", 400)
    sub, params, _ = pending
    password = form.get("password", "")
    if password != form.get("confirm"):
        return _new_password_form(challenge, "The passwords do not match.", 400)
    if problem := _password_problem(password):
        return _new_password_form(challenge, problem, 400)
    user = STATE.users[sub]
    user.password, user.status = _hash_password(password), "CONFIRMED"
    STATE.save()
    del STATE.challenges[challenge]
    return _issue_code(user, params)


def _client_ok(request: Request, form: dict[str, str]) -> bool:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
    else:
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
    return cid == CLIENT_ID and hmac.compare_digest(secret.encode(), CLIENT_SECRET.encode())


def _sign(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, STATE.key, algorithm="RS256", headers={"kid": STATE.kid})


async def token(request: Request) -> Response:
    form = await _form(request)
    if not _client_ok(request, form):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    grant = STATE.codes.pop(form.get("code", ""), None)  # single use
    verifier = form.get("code_verifier", "")
    if (
        grant is None
        or grant["expires"] < time.time()
        or grant["redirect_uri"] != form.get("redirect_uri")
        or _b64(hashlib.sha256(verifier.encode()).digest()) != grant["challenge"]
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    user = STATE.users.get(grant["sub"])
    if user is None or not user.enabled:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user.sub,
        "email": user.email,
        "email_verified": user.attributes.get("email_verified", "false") == "true",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "auth_time": now,
        "iat": now,
        "exp": now + ID_TOKEN_SECONDS,
        "cognito:username": user.sub,
    }
    if grant["nonce"]:
        claims["nonce"] = grant["nonce"]
    if user.groups:
        claims["cognito:groups"] = sorted(user.groups)
    for name, value in user.attributes.items():
        if name.startswith("custom:"):
            claims[name] = value
    access = {"sub": user.sub, "iss": ISSUER, "client_id": CLIENT_ID, "token_use": "access",
              "scope": "openid email", "iat": now, "exp": now + ID_TOKEN_SECONDS}
    return JSONResponse(
        {
            "id_token": _sign(claims),
            "access_token": _sign(access),
            "expires_in": ID_TOKEN_SECONDS,
            "token_type": "Bearer",
        },
        headers={"Cache-Control": "no-store"},
    )


async def logout(request: Request) -> Response:
    if request.query_params.get("client_id") != CLIENT_ID:
        return _page("Error", "<h1>Invalid request</h1><p>Unknown client_id.</p>", 400)
    target = request.query_params.get("logout_uri", "")
    if target not in LOGOUT_URLS:
        return _page("Error", "<h1>Invalid request</h1><p>logout_uri is not allowed.</p>", 400)
    STATE.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


async def jwks(request: Request) -> Response:
    if request.path_params["pool"] != POOL_ID:
        return JSONResponse({"message": "pool not found"}, status_code=404)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(STATE.key.public_key(), as_dict=True)  # type: ignore[union-attr]
    return JSONResponse({"keys": [{**public, "kid": STATE.kid, "alg": "RS256", "use": "sig"}]})


async def discovery(request: Request) -> Response:
    if request.path_params["pool"] != POOL_ID:
        return JSONResponse({"message": "pool not found"}, status_code=404)
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{base}/oauth2/authorize",
        "token_endpoint": f"{base}/oauth2/token",
        "jwks_uri": f"{base}/{POOL_ID}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
    })


# ------------------------------------------------------------------------------ admin API


class AwsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _user_or_raise(body: dict[str, Any]) -> User:
    if body.get("UserPoolId") != POOL_ID:
        raise AwsError("ResourceNotFoundException", f"User pool {body.get('UserPoolId')} does not exist.")
    user = STATE.find(str(body.get("Username", "")))
    if user is None:
        raise AwsError("UserNotFoundException", "User does not exist.")
    return user


def _send_invitation(user: User, password: str) -> None:
    STATE.outbox.append({
        "to": user.email,
        "subject": "Your zip gateway account",
        "sent": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "body": (
            f"Your zip gateway account is ready.\n\nSign in at {LOGIN_URL} with\n"
            f"email: {user.email}\ntemporary password: {password}\n\n"
            "You will choose your own password when you first sign in."
        ),
        "username": user.email,
        "temporary_password": password,
    })


def admin_create_user(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("UserPoolId") != POOL_ID:
        raise AwsError("ResourceNotFoundException", f"User pool {body.get('UserPoolId')} does not exist.")
    attrs = {a["Name"]: a["Value"] for a in body.get("UserAttributes", [])}
    email = str(attrs.get("email") or body.get("Username", "")).strip().lower()
    if "@" not in email:
        raise AwsError("InvalidParameterException", "Username should be an email.")
    existing = STATE.find(email)
    if existing is not None and body.get("MessageAction") != "RESEND":
        raise AwsError("UsernameExistsException", "An account with the given email already exists.")
    password = body.get("TemporaryPassword") or _temporary_password()
    user = existing or User(sub=str(uuid.uuid4()), email=email, created=time.time())
    user.password, user.status = _hash_password(password), "FORCE_CHANGE_PASSWORD"
    for name, value in attrs.items():
        if name.startswith("custom:") or name == "email_verified":
            user.attributes[name] = value
    STATE.users[user.sub] = user
    if body.get("MessageAction") != "SUPPRESS":
        _send_invitation(user, password)
    STATE.save()
    return {"User": {"Username": user.sub, "Attributes": _attributes(user),
                     "UserCreateDate": user.created, "UserLastModifiedDate": time.time(),
                     "Enabled": user.enabled, "UserStatus": user.status}}


def admin_get_user(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    return {"Username": user.sub, "UserAttributes": _attributes(user),
            "UserCreateDate": user.created, "UserLastModifiedDate": user.created,
            "Enabled": user.enabled, "UserStatus": user.status}


def admin_set_enabled(enabled: bool) -> Any:
    def handler(body: dict[str, Any]) -> dict[str, Any]:
        user = _user_or_raise(body)
        user.enabled = enabled
        STATE.save()
        return {}
    return handler


def admin_set_user_password(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    password = str(body.get("Password", ""))
    if problem := _password_problem(password):
        raise AwsError("InvalidPasswordException", f"Password does not conform to policy: {problem}")
    user.password = _hash_password(password)
    user.status = "CONFIRMED" if body.get("Permanent") else "FORCE_CHANGE_PASSWORD"
    STATE.save()
    return {}


def admin_delete_user(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    del STATE.users[user.sub]
    STATE.save()
    return {}


def admin_global_sign_out(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    user.session_version += 1
    STATE.save()
    return {}


def admin_list_groups_for_user(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    return {"Groups": [{"GroupName": g, "UserPoolId": POOL_ID} for g in sorted(user.groups)]}


def admin_add_user_to_group(body: dict[str, Any]) -> dict[str, Any]:
    user = _user_or_raise(body)
    if body.get("GroupName") not in GROUPS:
        raise AwsError("ResourceNotFoundException", "Group not found.")
    if body["GroupName"] not in user.groups:
        user.groups.append(body["GroupName"])
        STATE.save()
    return {}


OPERATIONS = {
    "AdminCreateUser": admin_create_user,
    "AdminGetUser": admin_get_user,
    "AdminDisableUser": admin_set_enabled(False),
    "AdminEnableUser": admin_set_enabled(True),
    "AdminUserGlobalSignOut": admin_global_sign_out,
    "AdminSetUserPassword": admin_set_user_password,
    "AdminDeleteUser": admin_delete_user,
    "AdminAddUserToGroup": admin_add_user_to_group,
    "AdminListGroupsForUser": admin_list_groups_for_user,
}


async def admin_api(request: Request) -> Response:
    target = request.headers.get("x-amz-target", "")
    service, _, operation = target.partition(".")
    headers = {"Content-Type": "application/x-amz-json-1.1"}
    try:
        if service != "AWSCognitoIdentityProviderService" or operation not in OPERATIONS:
            raise AwsError("UnsupportedOperationException", f"{target or 'no target'} is not supported here.")
        body = json.loads(await request.body() or b"{}")
        result = OPERATIONS[operation](body)
    except AwsError as exc:
        return Response(json.dumps({"__type": exc.code, "message": exc.message}), status_code=400,
                        headers={**headers, "x-amzn-ErrorType": exc.code})
    except ValueError:
        return Response(json.dumps({"__type": "SerializationException", "message": "bad JSON"}),
                        status_code=400, headers={**headers, "x-amzn-ErrorType": "SerializationException"})
    return Response(json.dumps(result), headers=headers)


# ------------------------------------------------------------------------------ outbox


async def outbox(request: Request) -> Response:
    rows = "".join(
        f"<tr><td>{html.escape(m['sent'])}</td><td>{html.escape(m['to'])}</td>"
        f"<td><pre>{html.escape(m['body'])}</pre></td></tr>"
        for m in reversed(STATE.outbox)
    )
    table = (f"<table><tr><th>Sent</th><th>To</th><th>Message</th></tr>{rows}</table>"
             if rows else "<p>No emails yet.</p>")
    return _page("Outbox", f"<h1>Outbox</h1><p>Emails Cognito would have sent.</p>{table}")


async def outbox_json(request: Request) -> Response:
    return JSONResponse(list(reversed(STATE.outbox)))


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


app = Starlette(routes=[
    Route("/", admin_api, methods=["POST"]),
    Route("/", outbox, methods=["GET"]),
    Route("/health", health),
    Route("/oauth2/authorize", authorize),
    Route("/login", login, methods=["POST"]),
    Route("/login/new-password", new_password, methods=["POST"]),
    Route("/oauth2/token", token, methods=["POST"]),
    Route("/logout", logout),
    Route("/outbox", outbox),
    Route("/outbox.json", outbox_json),
    Route("/{pool}/.well-known/jwks.json", jwks),
    Route("/{pool}/.well-known/openid-configuration", discovery),
])
