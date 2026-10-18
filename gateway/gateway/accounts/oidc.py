"""Sign-in with Amazon Cognito's hosted login: OpenID Connect, authorization code + PKCE.

    /account/auth/start      state, nonce and a PKCE verifier go into a short-lived signed
                          cookie (path /account/auth); the browser goes to Cognito
    /account/auth/callback   the code is exchanged at Cognito's token endpoint (with the
                          client secret and the PKCE verifier); the ID token is verified
                          here (RS256 against Cognito's JWKS, issuer, audience, expiry,
                          token_use, nonce) and becomes the gateway's own session cookie

Nothing from Cognito is kept after sign-in: the gateway session is independent of
Cognito's tokens, and signing out clears it and Cognito's hosted-login session.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

FLOW_COOKIE = "gw_oidc_flow"
FLOW_PATH = "/account/auth"
FLOW_SECONDS = 600
JWKS_SECONDS = 3600


class OidcError(Exception):
    """Sign-in failed. The message is safe to show the user."""


@dataclass(frozen=True)
class CognitoSettings:
    domain: str  # https://<prefix>.auth.<region>.amazoncognito.com
    client_id: str
    client_secret: str
    user_pool_id: str  # <region>_<id>
    # Server-to-server base URL (token endpoint, JWKS, admin API) when it differs from what
    # the browser uses: the compose name of the local fake-cognito. None on AWS.
    backchannel_url: str | None = None

    @property
    def region(self) -> str:
        return self.user_pool_id.split("_", 1)[0]

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def token_url(self) -> str:
        return f"{self.backchannel_url or self.domain}/oauth2/token"

    @property
    def jwks_url(self) -> str:
        if self.backchannel_url:
            return f"{self.backchannel_url}/{self.user_pool_id}/.well-known/jwks.json"
        return f"{self.issuer}/.well-known/jwks.json"


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    admin: bool
    invite_id: str | None


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    verifier: str
    next: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FlowCookie:
    """The pending sign-in, signed with the session secret so it cannot be forged."""

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(b"oidc-flow:" + secret.encode()).digest()

    def new(self, next_path: str) -> tuple[Flow, str]:
        flow = Flow(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            verifier=secrets.token_urlsafe(48),
            next=next_path,
        )
        payload = _b64(
            json.dumps({**flow.__dict__, "exp": int(time.time()) + FLOW_SECONDS}).encode()
        )
        return flow, f"{payload}.{self._sign(payload)}"

    def read(self, value: str | None) -> Flow | None:
        if not value or value.count(".") != 1:
            return None
        payload, signature = value.split(".")
        if not hmac.compare_digest(signature.encode(), self._sign(payload).encode()):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if int(data.pop("exp")) < time.time():
                return None
            return Flow(**{k: str(data[k]) for k in ("state", "nonce", "verifier", "next")})
        except (ValueError, KeyError, TypeError):
            return None

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())


class CognitoOidc:
    def __init__(self, settings: CognitoSettings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=10)
        self._jwks: dict[str, Any] = {}
        self._jwks_expires = 0.0

    def authorize_url(self, flow: Flow, redirect_uri: str) -> str:
        challenge = _b64(hashlib.sha256(flow.verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid email",
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._settings.domain}/oauth2/authorize?{query}"

    def logout_url(self, logout_uri: str) -> str:
        query = urlencode({"client_id": self._settings.client_id, "logout_uri": logout_uri})
        return f"{self._settings.domain}/logout?{query}"

    async def sign_in(self, code: str, flow: Flow, redirect_uri: str) -> Identity:
        """Exchange the authorization code and verify the ID token it returns."""
        try:
            response = await self._http.post(
                self._settings.token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": flow.verifier,
                },
                auth=(self._settings.client_id, self._settings.client_secret),
            )
        except httpx.HTTPError as exc:
            raise OidcError("Could not reach the sign-in service.") from exc
        if response.status_code != 200:
            raise OidcError("The sign-in could not be completed. Please try again.")
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("The sign-in service returned no identity.")
        return await self.verify(id_token, flow.nonce)

    async def verify(self, id_token: str, nonce: str) -> Identity:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
            key = await self._signing_key(str(kid))
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self._settings.client_id,
                issuer=self._settings.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise OidcError("The identity could not be verified.") from exc
        if claims.get("token_use") != "id":
            raise OidcError("The identity could not be verified.")
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise OidcError("The identity could not be verified.")
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise OidcError("The account has no email address.")
        groups = claims.get("cognito:groups") or []
        invite = claims.get("custom:invite_id")
        return Identity(
            sub=str(claims["sub"]),
            email=email.lower(),
            admin="admin" in groups,
            invite_id=invite if isinstance(invite, str) and invite else None,
        )

    async def _signing_key(self, kid: str) -> Any:
        if time.monotonic() >= self._jwks_expires or kid not in self._jwks:
            # Refetch on expiry, and once on an unknown kid (Cognito rotated its keys).
            response = await self._http.get(self._settings.jwks_url)
            response.raise_for_status()
            self._jwks = {key["kid"]: key for key in response.json()["keys"]}
            self._jwks_expires = time.monotonic() + JWKS_SECONDS
        return jwt.PyJWK(self._jwks[kid]).key
