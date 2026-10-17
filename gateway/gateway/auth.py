"""API-key authentication and the docs session cookie.

Two credentials, deliberately kept apart:

* API calls (`/<service>/...`) need `Authorization: Bearer <key>`. Cookies are never
  accepted there, so a logged-in browser cannot be tricked into uploading (no CSRF).
* The docs portal (`/docs`) is for browsers, which cannot attach a bearer header to a
  page load. Signing in with an API key sets a signed, HttpOnly cookie scoped to `/docs`.

Keys are compared by SHA-256 digest with `hmac.compare_digest`, checking every key so the
time taken does not reveal which one nearly matched.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

SESSION_COOKIE = "gw_docs_session"
SESSION_PATH = "/docs"


class KeyRing:
    def __init__(self, keys: Mapping[str, str]) -> None:
        self._digests = {name: _digest(secret) for name, secret in keys.items()}

    def __contains__(self, name: object) -> bool:
        return name in self._digests

    def identify(self, presented: str) -> str | None:
        """Return the name of the key matching `presented`, or None."""
        candidate = _digest(presented)
        match = None
        for name, digest in self._digests.items():
            if hmac.compare_digest(candidate, digest):
                match = name
        return match


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


@dataclass(frozen=True)
class Session:
    """Who is signed in to the portal."""

    owner: str  # "op:<operator key name>" (API-key sign-in) or "user:<cognito sub>"
    label: str  # what pages show: the key name, or the user's email
    admin: bool = False  # members of the Cognito "admin" group

    @property
    def operator_key(self) -> str | None:
        return self.owner[3:] if self.owner.startswith("op:") else None

    @property
    def user_sub(self) -> str | None:
        return self.owner[5:] if self.owner.startswith("user:") else None


class SessionSigner:
    """Stateless session tokens: `<payload>.<signature>`, both base64url.

    An operator session names the API key used to sign in, not the key itself: removing
    the key from configuration ends its sessions. A user session carries the Cognito sub,
    email and admin flag; disabling the user ends it (checked by the portal per request).
    """

    def __init__(self, secret: str, ttl_seconds: int, keyring: KeyRing) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_seconds
        self._keyring = keyring

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def issue(self, who: Session | str, now: float | None = None) -> str:
        """A token for `who` (a bare string is an operator key name)."""
        session = who if isinstance(who, Session) else Session(f"op:{who}", who)
        issued = int(time.time() if now is None else now)
        claims = {"o": session.owner, "l": session.label, "a": session.admin}
        payload = _b64encode(
            json.dumps({**claims, "exp": issued + self._ttl}, separators=(",", ":")).encode()
        )
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None, now: float | None = None) -> Session | None:
        """The session for a valid, unexpired token, or None."""
        if not token or token.count(".") != 1:
            return None
        payload, signature = token.split(".")
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            claims = json.loads(_b64decode(payload))
            owner, label, admin, expires = claims["o"], claims["l"], claims["a"], claims["exp"]
        except (ValueError, KeyError, TypeError):
            return None
        if not (
            isinstance(owner, str)
            and isinstance(label, str)
            and isinstance(admin, bool)
            and isinstance(expires, int)
        ):
            return None
        if expires <= (time.time() if now is None else now):
            return None
        session = Session(owner, label, admin)
        if session.operator_key is not None and session.operator_key not in self._keyring:
            return None
        if session.operator_key is None and session.user_sub is None:
            return None
        return session

    def csrf_token(self, session_token: str) -> str:
        """A per-session token for state-changing browser requests (double submit).

        Derived from the session cookie, so it needs no storage, changes with every
        sign-in, and cannot be computed by a page that cannot read the cookie.
        """
        digest = hmac.new(self._secret, b"csrf:" + session_token.encode(), hashlib.sha256)
        return _b64encode(digest.digest())

    def check_csrf(self, session_token: str | None, presented: str | None) -> bool:
        if not session_token or not presented:
            return False
        # As bytes: compare_digest rejects non-ASCII str, and the header is client input.
        return hmac.compare_digest(presented.encode(), self.csrf_token(session_token).encode())

    def _sign(self, payload: str) -> str:
        return _b64encode(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
