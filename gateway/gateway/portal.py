"""The gateway's own routes: health checks, the portal, and its documentation.

    GET  /health/live, /health/ready          unauthenticated, for the load balancer
    GET  /                                   home: services, status, versions
    GET  /zip                                upload page (+ /zip/jobs/..., browser_jobs.py)
    GET  /account/login, POST /account/login  sign-in page; sign-in with an API key
    GET  /account/auth/start, /account/auth/callback  sign-in with Cognito (accounts/oidc.py)
    POST /account/logout
    GET|POST /account/join                   public: redeem an invite code (choose email, password)
    GET  /account/keys                       the user's own API keys
    GET  /account/admin/{invites,users,keys}  admin pages (Cognito group "admin")
    GET  /docs                               documentation index
    GET  /docs/api                           interactive reference (Scalar) for the contract
    GET  /docs/api/<service>[?source=contract] a service's own document, or the contract
    GET  /docs/openapi/...json               those documents, prepared for the gateway
    GET  /docs/openapi.yaml                  the contract's document, as written
    GET  /docs/compare                       each service's document against the contract
    GET  /docs/pages/<slug>                  the gateway's pages (contract, usage)
    GET  /docs/services/<service>/<slug>     a service's own docs (contract §14)
    GET  /static/<name>                      the portal's CSS and scripts

`/docs` holds documentation only. Every page except health, sign-in and joining with an invite code
needs the session cookie (see auth.py); the browser is sent to the login page
otherwise. Pages are server-rendered with no inline script, so a strict CSP applies to all
of them except the API reference, which loads Scalar from a pinned, SRI-checked URL.

The session cookie is SameSite=Lax, not Strict: signing in with Cognito ends in a
redirect chain started on Cognito's site, and a Strict cookie would not be sent on it.
Cross-site POSTs still never carry it, and every state-changing request also needs a
same-origin `Origin` and the session's CSRF token.
"""

import asyncio
import hmac
import html
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from gateway.accounts import pages
from gateway.accounts.directory import DirectoryError
from gateway.accounts.invites import InvalidInvite, Redeemed, password_problem, valid_email
from gateway.accounts.keys import InvalidKeyName, TooManyKeys, utc_now
from gateway.accounts.oidc import FLOW_COOKIE, FLOW_PATH, FLOW_SECONDS, OidcError
from gateway.accounts.quota import human_bytes
from gateway.accounts.service import Accounts
from gateway.accounts.store import ApiKeyRecord
from gateway.auth import SESSION_COOKIE, SESSION_PATH, KeyRing, Session, SessionSigner
from gateway.browser_jobs import AsgiEndpoint, BrowserJobs
from gateway.config import Settings, Upstream
from gateway.docs import DocsSite
from gateway.openapi_view import compare, for_gateway
from gateway.origin import public_origin, same_origin
from gateway.problems import problem_response
from gateway.ratelimit import RateLimiter
from gateway.service_docs import ServiceDocs, ServiceDocsCache
from gateway.upstream import fetch

logger = logging.getLogger("gateway.portal")

# Pinned version and Subresource Integrity hash: the CDN cannot change what runs here.
SCALAR_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.64.0/dist/browser/standalone.js"
SCALAR_SRI = "sha384-ei8P62VHbV+6AdLO3hN333PsTEYp6k9OAVhlYvpmer+zdPIf8jSbdgh9ojiLWX3T"

_STRICT_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
# Scalar injects inline styles. `connect-src 'self'` keeps "try it" requests, and the API
# key typed into them, on this origin: no third-party request proxy can be used. Scripts:
# this release's own directory on the CDN only, not all of jsdelivr (which serves anyone's
# npm and GitHub files, so allowing the host would let injected markup load any script).
SCALAR_SCRIPT_SOURCE = SCALAR_URL.rsplit("/dist/", 1)[0] + "/"
_SCALAR_CSP = (
    f"default-src 'none'; script-src 'self' {SCALAR_SCRIPT_SOURCE}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# The upload page is the only portal page with a script: its own file (no inline code),
# talking to this origin only, with a same-origin frame for the archive download.
_ZIP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": with it, browsers send `Origin: null` on form posts, which the
    # same-origin check then refuses. "same-origin" still sends nothing to other sites.
    "Referrer-Policy": "same-origin",
}
_NEXT = re.compile(r"^/((docs|zip|account/keys|account/admin)(/[A-Za-z0-9._/-]*)?)?$")
_MAX_LOGIN_BODY = 4096
# Unauthenticated form posts (key sign-in, invite redemption) per client IP address.
_FORM_POSTS_PER_MINUTE = 10
_STATUS_TIMEOUT_SECONDS = 2.0


def _safe_next(value: str | None) -> str:
    """Only redirect within the portal after sign-in (no open redirect)."""
    return value if value and _NEXT.fullmatch(value) else "/"


def create_portal(
    settings: Settings,
    keyring: KeyRing,
    sessions: SessionSigner,
    docs: DocsSite,
    service_docs: ServiceDocsCache,
    jobs: BrowserJobs,
    accounts: Accounts,
    lifespan: Callable[[Any], AbstractAsyncContextManager[None]] | None = None,
) -> Starlette:
    keys = accounts.keys
    oidc = accounts.oidc
    form_posts = RateLimiter(_FORM_POSTS_PER_MINUTE, 60)

    def too_many(request: Request) -> Response | None:
        """429 if this client has posted too many unauthenticated forms lately."""
        wait = form_posts.retry_after(_client_ip(request) or "unknown")
        if wait is None:
            return None
        response = message_page(
            "Too many attempts", f"Please wait {wait} seconds and try again.", status=429
        )
        response.headers["Retry-After"] = str(wait)
        return response

    async def current(request: Request) -> tuple[Session, str] | None:
        """The signed-in session and its token, or None (also for a disabled user)."""
        token = request.cookies.get(SESSION_COOKIE)
        session = sessions.verify(token)
        if session is None or token is None:
            return None
        live = await accounts.users.check(session)  # disabled, signed out, admin changed
        return None if live is None else (live, token)

    async def current_admin(request: Request) -> tuple[Session, str] | Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        if not me[0].admin:
            raise HTTPException(403)
        return me

    def login_redirect(request: Request) -> Response:
        return RedirectResponse(f"/account/login?next={quote(request.url.path)}", status_code=303)

    def html_page(
        title: str, body: str, nav: str = "", *, status: int = 200, csp: str = _STRICT_CSP
    ) -> HTMLResponse:
        return HTMLResponse(
            _layout(title, body, nav),
            status_code=status,
            headers={**_SECURITY_HEADERS, "Content-Security-Policy": csp},
        )

    async def nav_for(session: Session, all_docs: Sequence[ServiceDocs] | None = None) -> str:
        found = await service_docs.get_all() if all_docs is None else all_docs
        return _nav(docs, found, admin=session.admin)

    async def portal_page(
        session: Session, title: str, body: str, *, status: int = 200
    ) -> HTMLResponse:
        return html_page(title, body, await nav_for(session), status=status)

    def public_base(request: Request) -> str:
        return settings.public_url or _base_url(request)

    def set_session(response: Response, session: Session) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            sessions.issue(session),
            max_age=sessions.ttl_seconds,
            path=SESSION_PATH,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def form_allowed(request: Request, form: dict[str, str] | None, token: str) -> bool:
        """Same-origin, and carrying this session's CSRF token."""
        return (
            form is not None
            and same_origin(request.headers, request.url.scheme)
            and sessions.check_csrf(token, form.get("csrf"))
        )

    def login_page(next_path: str, error: str | None = None, *, status: int = 200) -> Response:
        body = pages.login_body(
            _safe_next(next_path),
            error,
            cognito=oidc is not None,
            key_login=settings.allow_key_login,
        )
        return html_page("Sign in", body, status=status)

    def message_page(
        title: str, text: str, *, status: int = 200, link: tuple[str, str] | None = None
    ) -> Response:
        return html_page(title, pages.message_body(title, text, link=link), status=status)

    # ------------------------------------------------------------------ health

    async def live(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(request: Request) -> Response:
        return JSONResponse({"status": "ready"})

    # ------------------------------------------------------------------ signing in and out

    async def login_form(request: Request) -> Response:
        return login_page(request.query_params.get("next", "/"))

    async def login(request: Request) -> Response:
        """Sign in with an operator API key (GATEWAY_ALLOW_KEY_LOGIN)."""
        if not settings.allow_key_login:
            raise HTTPException(404)
        if (limited := too_many(request)) is not None:
            return limited
        if not same_origin(request.headers, request.url.scheme):
            raise HTTPException(403)  # no signing a visitor in from another site
        form = await _read_form(request, _MAX_LOGIN_BODY)
        if form is None:
            return login_page("/", "Request too large.", status=413)
        target = _safe_next(form.get("next"))
        key_name = keyring.identify(form.get("api_key", "").strip())
        if key_name is None:
            return login_page(target, "That API key is not valid.", status=401)
        response = RedirectResponse(target, status_code=303)
        set_session(response, Session(f"op:{key_name}", key_name))
        return response

    async def auth_start(request: Request) -> Response:
        if oidc is None or settings.public_url is None:
            raise HTTPException(404)
        flow, cookie = accounts.flow.new(_safe_next(request.query_params.get("next")))
        redirect_uri = f"{settings.public_url}/account/auth/callback"
        response = RedirectResponse(oidc.authorize_url(flow, redirect_uri), status_code=303)
        response.set_cookie(
            FLOW_COOKIE,
            cookie,
            max_age=FLOW_SECONDS,
            path=FLOW_PATH,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",  # sent on the top-level redirect back from Cognito
        )
        return response

    async def auth_callback(request: Request) -> Response:
        if oidc is None or settings.public_url is None:
            raise HTTPException(404)
        params = request.query_params
        flow = accounts.flow.read(request.cookies.get(FLOW_COOKIE))
        state = params.get("state", "")
        if flow is None or not hmac.compare_digest(state.encode(), flow.state.encode()):
            return login_page("/", "That sign-in expired. Please try again.", status=400)
        if params.get("error") or not params.get("code"):
            # e.g. the user cancelled on Cognito's page
            return login_page(flow.next, "Sign-in was not completed.", status=400)
        redirect_uri = f"{settings.public_url}/account/auth/callback"
        try:
            identity = await oidc.sign_in(params["code"], flow, redirect_uri)
        except OidcError as exc:
            logger.warning("sign-in failed", extra={"reason": str(exc)})
            return login_page(flow.next, str(exc), status=400)
        existing = await accounts.store.get_user(identity.sub)
        if existing is not None and not existing.active:
            return login_page("/", "This account has been disabled.", status=403)
        await accounts.store.record_login(
            identity.sub,
            identity.email,
            identity.invite_id,
            utc_now(),
            _client_ip(request),
            request.headers.get("user-agent"),
        )
        accounts.users.forget(identity.sub)
        response = RedirectResponse(flow.next, status_code=303)
        set_session(response, Session(f"user:{identity.sub}", identity.email, identity.admin))
        response.delete_cookie(FLOW_COOKIE, path=FLOW_PATH)
        return response

    async def logout(request: Request) -> Response:
        if not same_origin(request.headers, request.url.scheme):
            raise HTTPException(403)  # no forced sign-out from another site
        session = sessions.verify(request.cookies.get(SESSION_COOKIE))
        if session is not None and session.user_sub:
            # End every copy of this session, not just the browser's: a copied cookie
            # would otherwise stay valid until it expires.
            await accounts.store.end_sessions(session.user_sub, datetime.now(UTC))
            accounts.users.forget(session.user_sub)
        target = "/account/login"
        if session is not None and session.user_sub and oidc and settings.public_url:
            # Also end Cognito's own session, or "Sign in" would skip its login form.
            target = oidc.logout_url(f"{settings.public_url}/account/login")
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
        return response

    # ------------------------------------------------------------------ joining (public)

    async def join(request: Request) -> Response:
        """Redeem an invite code: the person chooses their email and password here."""
        if not accounts.invites.enabled:
            return message_page(
                "Sign-up unavailable", "Accounts cannot be created here.", status=404
            )
        if request.method == "GET":
            code = request.query_params.get("code", "")[:20]
            return html_page("Create your account", pages.join_form(code))
        if (limited := too_many(request)) is not None:
            return limited
        form = await _read_form(request)
        if form is None or not same_origin(request.headers, request.url.scheme):
            raise HTTPException(403)
        code = form.get("code", "").strip()[:20]
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")

        def again(error: str, status: int) -> Response:
            # The code and email are kept; passwords never go back into a page.
            body = pages.join_form(code, email, error)
            return html_page("Create your account", body, status=status)

        if not valid_email(email):
            return again("Enter a valid email address.", 422)
        if password != form.get("confirm", ""):
            return again("The two passwords do not match.", 422)
        if (problem := password_problem(password)) is not None:
            return again(f"Choose another password: {problem}", 422)
        outcome = await accounts.invites.redeem(code, email, password)
        if outcome is Redeemed.CREATED:
            return message_page(
                "Your account is ready",
                f"Sign in as {email} with the password you just chose.",
                link=("/account/auth/start?next=/", "Sign in"),
            )
        if outcome is Redeemed.INVALID:
            return again(
                "That code is not valid: it may have been used, have expired or been "
                "withdrawn. Check it, or ask whoever gave it to you for a new one.",
                410,
            )
        if outcome is Redeemed.EXISTS:
            return again("That email already has an account: sign in instead, or use another.", 409)
        if outcome is Redeemed.BAD_PASSWORD:
            return again("That password was not accepted: choose another.", 422)
        return again("Your account could not be created just now. The code still works.", 503)

    # ------------------------------------------------------------------ docs pages

    async def home(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        session = me[0]
        upstreams = list(settings.upstreams.values())
        statuses, all_docs = await asyncio.gather(
            asyncio.gather(*(_probe(u) for u in upstreams)), service_docs.get_all()
        )
        body = _home_body(session.label, upstreams, statuses, all_docs)
        return html_page("Zip services", body, await nav_for(session, all_docs))

    async def docs_index(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        all_docs = await service_docs.get_all()
        body = _docs_index_body(docs, all_docs)
        return html_page("Documentation", body, await nav_for(me[0], all_docs))

    async def api_reference(request: Request) -> Response:
        if await current(request) is None:
            return login_redirect(request)
        service = request.path_params.get("service")
        if service is None:
            spec_url, bar, title = "/docs/openapi/contract.json", _spec_bar(), "API contract"
        else:
            if service not in settings.upstreams:
                raise HTTPException(404)
            published = await service_docs.get(service)
            # A service's own document by default; the contract if asked or if it has none.
            wants_contract = request.query_params.get("source") == "contract"
            source = "contract" if wants_contract or published.spec is None else "service"
            spec_url = f"/docs/openapi/{service}/{source}.json"
            bar, title = _spec_bar(service, published, source), f"{service} API"
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)} - zip gateway</title>"
            '<link rel="stylesheet" href="/static/api-bar.css"></head><body>'
            # api-reference.js reads the document URL from this attribute (no inline script).
            f'{bar}<div id="api-reference" data-spec="{html.escape(spec_url)}"></div>'
            f'<script src="{SCALAR_URL}" integrity="{SCALAR_SRI}" crossorigin="anonymous">'
            '</script><script src="/static/api-reference.js"></script></body></html>',
            headers={**_SECURITY_HEADERS, "Content-Security-Policy": _SCALAR_CSP},
        )

    async def openapi(request: Request) -> Response:
        if await current(request) is None:
            return PlainTextResponse("Sign in at /account/login", status_code=401)
        return FileResponse(
            docs.openapi_path, media_type="application/yaml", headers=_SECURITY_HEADERS
        )

    async def contract_spec(request: Request) -> Response:
        if await current(request) is None:
            return PlainTextResponse("Sign in at /account/login", status_code=401)
        base = _base_url(request)
        servers = [
            (f"{base}/{name}", f"{name}, through the gateway") for name in settings.upstreams
        ]
        note = (
            "**The contract**: what every implementation must do. Each service also "
            "publishes what it actually does; compare them on the portal's Compare page."
        )
        document = for_gateway(docs.contract_spec, servers, note)
        return JSONResponse(document, headers=_SECURITY_HEADERS)

    async def service_spec(request: Request) -> Response:
        if await current(request) is None:
            return PlainTextResponse("Sign in at /account/login", status_code=401)
        service, source = request.path_params["service"], request.path_params["source"]
        if service not in settings.upstreams or source not in ("service", "contract"):
            raise HTTPException(404)
        published = await service_docs.get(service)
        if source == "service":
            if published.spec is None:
                raise HTTPException(404)
            spec = published.spec
            note = (
                f"**Published by {service} itself**{_build_label(published)}, generated "
                "from its code. Shown through the gateway: the server, authentication and "
                "the gateway's own errors (401, 429, 502, 504) were added."
            )
        else:
            spec = docs.contract_spec
            note = f"**The contract**, applied to {service}. What the service must do."
        server = [(f"{_base_url(request)}/{service}", f"{service}, through the gateway")]
        return JSONResponse(for_gateway(spec, server, note), headers=_SECURITY_HEADERS)

    async def compare_page(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        all_docs = await service_docs.get_all()
        body = _compare_body(docs.contract_spec, all_docs)
        return html_page("Compare specs", body, await nav_for(me[0], all_docs))

    async def gateway_page(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        found = docs.pages.get(request.path_params["slug"])
        if found is None:
            raise HTTPException(404)
        return await portal_page(me[0], found.title, f"<article>{found.html}</article>")

    async def service_page(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        service = request.path_params["service"]
        if service not in settings.upstreams:
            raise HTTPException(404)
        service_doc_set = await service_docs.get(service)
        document = service_doc_set.documents.get(request.path_params["slug"])
        if document is None:
            raise HTTPException(404)
        crumb = html.escape(f"{service} - {document.title}{_build_label(service_doc_set)}")
        body = f'<p class="crumb">{crumb}</p><article>{document.html}</article>'
        return await portal_page(me[0], document.title, body)

    # ------------------------------------------------------------------ zipping and keys

    async def zip_page(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        session, token = me
        upstreams = list(settings.upstreams.values())
        statuses, all_docs = await asyncio.gather(
            asyncio.gather(*(_probe(u) for u in upstreams)), service_docs.get_all()
        )
        body = _zip_body(upstreams, statuses, all_docs, sessions.csrf_token(token))
        return html_page("Zip files", body, await nav_for(session, all_docs), csp=_ZIP_CSP)

    async def keys_page(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        session, token = me
        new_key, error, status = None, None, 200
        if request.method == "POST":
            form = await _read_form(request)
            if not form_allowed(request, form, token):
                raise HTTPException(403)
            assert form is not None
            try:
                _, new_key = await keys.create(session.owner, session.label, form.get("name", ""))
            except InvalidKeyName as exc:
                error, status = str(exc), 422
            except TooManyKeys:
                error, status = "You already have 5 active keys: revoke one first.", 409
        mine = await keys.list_keys(session.owner)
        usage = None
        if accounts.quota.applies(session.owner):
            usage = (await accounts.quota.used_today(session.owner), accounts.quota.daily_bytes)
        body = _keys_body(mine, sessions.csrf_token(token), new_key, error, usage)
        return await portal_page(session, "My API keys", body, status=status)

    async def revoke_key(request: Request) -> Response:
        me = await current(request)
        if me is None:
            return login_redirect(request)
        session, token = me
        if not form_allowed(request, await _read_form(request), token):
            raise HTTPException(403)
        if not await keys.revoke(request.path_params["prefix"], session.owner):
            raise HTTPException(404)
        return RedirectResponse("/account/keys", status_code=303)

    # ------------------------------------------------------------------ admin

    async def admin_invites(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        session, token = me
        new_code, error, status = None, None, 200
        if request.method == "POST":
            form = await _read_form(request)
            if not form_allowed(request, form, token):
                raise HTTPException(403)
            assert form is not None
            try:
                days = int(form.get("days", "7"))
                created = await accounts.invites.create(form.get("label", ""), days, session.label)
                new_code = created.code
            except ValueError as exc:  # InvalidInvite, or days not a number
                error = str(exc) if isinstance(exc, InvalidInvite) else "Enter a number of days."
                status = 422
        body = pages.admin_invites_body(
            await accounts.invites.list_invites(),
            sessions.csrf_token(token),
            new_code=new_code,
            join_url=f"{public_base(request)}/account/join",
            error=error,
            enabled=accounts.invites.enabled,
            now=utc_now(),
        )
        return await portal_page(session, "Invites", body, status=status)

    async def admin_revoke_invite(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        if not form_allowed(request, await _read_form(request), me[1]):
            raise HTTPException(403)
        if not await accounts.invites.revoke(request.path_params["invite_id"]):
            raise HTTPException(404)
        return RedirectResponse("/account/admin/invites", status_code=303)

    async def users_page(session: Session, token: str, error: str | None, status: int) -> Response:
        users, labels, logins = await asyncio.gather(
            accounts.store.list_users(),
            accounts.store.invite_labels(),
            accounts.store.recent_logins(),
        )
        body = pages.admin_users_body(
            users,
            labels,
            logins,
            sessions.csrf_token(token),
            error,
            usage=await accounts.quota.usage_all(),
            daily_quota=accounts.quota.daily_bytes,
        )
        return await portal_page(session, "Users", body, status=status)

    async def admin_users(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        return await users_page(*me, None, 200)

    async def admin_disable_user(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        session, token = me
        if not form_allowed(request, await _read_form(request), token):
            raise HTTPException(403)
        sub = request.path_params["sub"]
        if sub == session.user_sub:
            return await users_page(session, token, "You cannot disable yourself.", 409)
        if await accounts.store.get_user(sub) is None:
            raise HTTPException(404)
        if accounts.directory is not None:
            try:
                await accounts.directory.disable_user(sub)
            except DirectoryError as exc:
                logger.warning("could not disable user in Cognito", extra={"reason": str(exc)})
                error = "Cognito could not disable the user; nothing was changed. Try again."
                return await users_page(session, token, error, 502)
        await accounts.store.disable_user(sub, utc_now())  # also revokes their keys
        accounts.users.forget(sub)
        return RedirectResponse("/account/admin/users", status_code=303)

    async def admin_keys(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        session, token = me
        body = pages.admin_keys_body(await keys.list_keys(), sessions.csrf_token(token))
        return await portal_page(session, "API keys", body)

    async def admin_revoke_key(request: Request) -> Response:
        me = await current_admin(request)
        if isinstance(me, Response):
            return me
        if not form_allowed(request, await _read_form(request), me[1]):
            raise HTTPException(403)
        if not await keys.revoke(request.path_params["prefix"]):
            raise HTTPException(404)
        return RedirectResponse("/account/admin/keys", status_code=303)

    # ------------------------------------------------------------------ the rest

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in _STATIC:
            raise HTTPException(404)
        content, media_type = _STATIC[name]
        return Response(content, media_type=media_type, headers={"Cache-Control": "no-cache"})

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        codes = {403: "forbidden", 404: "not-found", 405: "method-not-allowed"}
        code = codes.get(exc.status_code, "internal-error")
        return problem_response(code, request.scope["state"]["request_id"], headers=exc.headers)

    async def internal_error(request: Request, exc: Exception) -> Response:
        return problem_response("internal-error", request.scope["state"]["request_id"])

    return Starlette(
        routes=[
            Route("/", home),
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/docs", docs_index),
            Route("/docs/api", api_reference),
            Route("/docs/api/{service}", api_reference),
            Route("/docs/openapi.yaml", openapi),
            Route("/docs/openapi/contract.json", contract_spec),
            Route("/docs/openapi/{service}/{source}.json", service_spec),
            Route("/docs/compare", compare_page),
            Route("/zip", zip_page),
            Route("/account/keys", keys_page, methods=["GET", "POST"]),
            Route("/account/keys/{prefix}/revoke", revoke_key, methods=["POST"]),
            Route("/zip/jobs", AsgiEndpoint(jobs.create), methods=["POST"]),
            Route("/zip/jobs/{job_id}/archive", AsgiEndpoint(jobs.download), methods=["GET"]),
            Route("/zip/jobs/{job_id}/files", AsgiEndpoint(jobs.upload), methods=["POST"]),
            Route("/docs/pages/{slug}", gateway_page),
            Route("/docs/services/{service}/{slug}", service_page),
            Route("/account/login", login_form, methods=["GET"]),
            Route("/account/login", login, methods=["POST"]),
            Route("/account/auth/start", auth_start, methods=["GET"]),
            Route("/account/auth/callback", auth_callback, methods=["GET"]),
            Route("/account/logout", logout, methods=["POST"]),
            Route("/account/join", join, methods=["GET", "POST"]),
            Route("/account/admin/invites", admin_invites, methods=["GET", "POST"]),
            Route(
                "/account/admin/invites/{invite_id}/revoke", admin_revoke_invite, methods=["POST"]
            ),
            Route("/account/admin/users", admin_users, methods=["GET"]),
            Route("/account/admin/users/{sub}/disable", admin_disable_user, methods=["POST"]),
            Route("/account/admin/keys", admin_keys, methods=["GET"]),
            Route("/account/admin/keys/{prefix}/revoke", admin_revoke_key, methods=["POST"]),
            Route("/static/{name}", static),
        ],
        exception_handlers={HTTPException: http_error, Exception: internal_error},
        lifespan=lifespan,
    )


def _client_ip(request: Request) -> str | None:
    """The browser's address: the last X-Forwarded-For entry is the one the load
    balancer (or Caddy) saw; earlier entries are whatever the client claimed."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or None
    return request.client.host if request.client else None


async def _probe(upstream: Upstream) -> tuple[str, float | None]:
    started = time.monotonic()
    try:
        status, _ = await fetch(upstream, "GET", "/health/ready", _STATUS_TIMEOUT_SECONDS)
    except Exception:  # refused, timed out, invalid HTTP: all mean "unreachable" here
        return "unreachable", None
    elapsed_ms = (time.monotonic() - started) * 1000
    return ("ready" if status == 200 else f"not ready ({status})"), elapsed_ms


def _build_label(service_docs: ServiceDocs) -> str:
    parts = [service_docs.version, (service_docs.revision or "")[:12] or None]
    label = " ".join(part for part in parts if part)
    return f" ({label})" if label else ""


def _base_url(request: Request) -> str:
    """The public origin for OpenAPI server URLs; "" (relative URLs) if not determinable."""
    return public_origin(request.headers, request.url.scheme)


def _spec_bar(
    service: str | None = None, published: ServiceDocs | None = None, source: str = ""
) -> str:
    back = '<a href="/docs">&larr; Docs</a>'
    if service is None or published is None:
        return (
            f'<nav class="spec-bar">{back}<span><span class="source">The contract</span>, '
            "for every service: pick one in the server list. Each service's own document is "
            'linked from the docs home; <a href="/docs/compare">compare them</a>.</span></nav>'
        )
    name = html.escape(service)
    compare = f'<a href="/docs/compare#{name}">differences from the contract</a>'
    if source == "service":
        build = html.escape(_build_label(published).strip())
        shown = f'<span class="source">{name}\'s own document</span> {build}'
        other = f'<a href="/docs/api/{name}?source=contract">show the contract instead</a>'
    else:
        shown = f'<span class="source">The contract</span>, applied to {name}'
        if published.spec is not None:
            other = f'<a href="/docs/api/{name}">show {name}\'s own document</a>'
        else:
            reason = published.spec_error or published.error or "none listed in its manifest"
            other = f"({name}'s own document is unavailable: {html.escape(reason)})"
    return (
        f'<nav class="spec-bar">{back}<span>Showing {shown}</span>'
        f"<span>{other}</span><span>{compare}</span></nav>"
    )


def _compare_body(contract: dict[str, Any], all_docs: Sequence[ServiceDocs]) -> str:
    parts = [
        "<h1>Compare specs</h1>"
        "<p>Each service's own OpenAPI document, generated from its code, against the "
        "contract's. Compared: operations, documented response statuses, request media "
        "types and fields, required fields, and success media types. A difference is a "
        "finding, not necessarily a bug: it shows where an implementation documents "
        "something other than the contract, or chose differently where the contract is "
        "silent.</p>"
    ]
    for service_docs in all_docs:
        name = html.escape(service_docs.service)
        links = (
            f'<p><a href="/docs/api/{name}">{name}\'s reference</a> &middot; '
            f'<a href="/docs/api/{name}?source=contract">the contract for {name}</a></p>'
        )
        parts.append(f'<h2 id="{name}">{name}{html.escape(_build_label(service_docs))}</h2>')
        if service_docs.spec is None:
            reason = service_docs.spec_error or service_docs.error or "none listed in its manifest"
            parts.append(
                f'<p class="muted">No OpenAPI document published ({html.escape(reason)}).</p>'
            )
            continue
        differences = compare(contract, service_docs.spec)
        if not differences:
            parts.append(f"<p>No differences in what is compared.</p>{links}")
            continue
        rows = "".join(
            "<tr>"
            f"<td><code>{html.escape(d.operation)}</code></td><td>{html.escape(d.aspect)}</td>"
            f"<td>{html.escape(d.contract) or '-'}</td><td>{html.escape(d.service) or '-'}</td>"
            "</tr>"
            for d in differences
        )
        parts.append(
            f"<p>{len(differences)} difference{'s' if len(differences) != 1 else ''}.</p>"
            '<div class="scroll"><table><thead><tr><th>Operation</th><th>Aspect</th>'
            f"<th>Contract</th><th>{name}</th></tr></thead><tbody>{rows}</tbody></table></div>"
            f"{links}"
        )
    return "".join(parts)


def _nav(docs: DocsSite, all_docs: Sequence[ServiceDocs], *, admin: bool = False) -> str:
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Gateway",
            [("/", "Home"), ("/zip", "Zip files"), ("/account/keys", "My API keys")],
        ),
        (
            "Docs",
            [
                ("/docs", "Documentation"),
                ("/docs/api", "API reference (all)"),
                ("/docs/compare", "Compare specs"),
            ]
            + [(f"/docs/pages/{p.slug}", p.title) for p in docs.pages.values()],
        ),
    ]
    for service_docs in all_docs:
        links = [(f"/docs/api/{service_docs.service}", "API reference")] + [
            (f"/docs/services/{service_docs.service}/{d.slug}", d.title)
            for d in service_docs.documents.values()
        ]
        sections.append((service_docs.service, links))
    parts = []
    for (heading, links), section_docs in zip(sections, [None, None, *all_docs], strict=True):
        items = "".join(
            f'<li><a href="{html.escape(href)}">{html.escape(title)}</a></li>'
            for href, title in links
        )
        if section_docs is not None and not section_docs.available:
            items += '<li class="muted">docs unavailable</li>'
        parts.append(f"<h3>{html.escape(heading)}</h3><ul>{items}</ul>")
    if admin:
        parts.append(
            "<h3>Admin</h3><ul>"
            '<li><a href="/account/admin/invites">Invites</a></li>'
            '<li><a href="/account/admin/users">Users</a></li>'
            '<li><a href="/account/admin/keys">API keys</a></li></ul>'
        )
    return "".join(parts)


def _docs_index_body(docs: DocsSite, all_docs: Sequence[ServiceDocs]) -> str:
    """/docs: every document, in one place."""

    def items(links: Sequence[tuple[str, str, str]]) -> str:
        return "".join(
            f'<li><a href="{html.escape(href)}">{html.escape(title)}</a>'
            f"{f' <span class="muted">{html.escape(note)}</span>' if note else ''}</li>"
            for href, title, note in links
        )

    gateway = [
        ("/docs/api", "API reference", "the contract, with every service as a server"),
        ("/docs/compare", "Compare specs", "where each service's document differs from it"),
        *[(f"/docs/pages/{p.slug}", p.title, "") for p in docs.pages.values()],
    ]
    parts = ["<h1>Documentation</h1>", "<h2>The API</h2>", f"<ul>{items(gateway)}</ul>"]
    for service_docs in all_docs:
        name = service_docs.service
        links = [(f"/docs/api/{name}", "API reference", _build_label(service_docs).strip(" ()"))]
        links += [
            (f"/docs/services/{name}/{d.slug}", d.title, "")
            for d in service_docs.documents.values()
        ]
        unavailable = (
            "" if service_docs.available else '<p class="muted">Its own docs are unavailable.</p>'
        )
        parts.append(f"<h2>{html.escape(name)}</h2><ul>{items(links)}</ul>{unavailable}")
    return "".join(parts)


def _layout(title: str, body: str, nav: str) -> str:
    header = (
        '<header><a class="brand" href="/">zip gateway</a>'
        '<a href="/zip">Zip files</a><a href="/docs">Docs</a>'
        '<form method="post" action="/account/logout"><button>Sign out</button></form></header>'
        if nav
        else '<header><span class="brand">zip gateway</span></header>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} - zip gateway</title>"
        '<link rel="stylesheet" href="/static/portal.css"></head><body>'
        f'{header}<div class="layout"><nav>{nav}</nav><main>{body}</main></div></body></html>'
    )


def _home_body(
    user: str,
    upstreams: Sequence[Upstream],
    statuses: Sequence[tuple[str, float | None]],
    all_docs: Sequence[ServiceDocs],
) -> str:
    rows = []
    for upstream, (state, latency_ms), service_docs in zip(
        upstreams, statuses, all_docs, strict=True
    ):
        css = "ok" if state == "ready" else "bad"
        build = _build_label(service_docs).strip(" ()") or "-"
        contract = service_docs.contract_version or "-"
        spec = "own document" if service_docs.spec is not None else "contract only"
        if service_docs.available:
            links = ", ".join(
                f'<a href="/docs/services/{html.escape(upstream.name)}/{d.slug}">'
                f"{html.escape(d.title)}</a>"
                for d in service_docs.documents.values()
            )
        else:
            reason = html.escape(service_docs.error or "")
            links = f'<span class="muted">unavailable: {reason}</span>'
        rows.append(
            "<tr>"
            f'<td><a href="/docs/api/{html.escape(upstream.name)}">'
            f"<code>/{html.escape(upstream.name)}/api/v1/zip</code></a></td>"
            f'<td class="{css}">{html.escape(state)}</td>'
            f"<td>{'-' if latency_ms is None else f'{latency_ms:.0f} ms'}</td>"
            f"<td>{html.escape(build)}</td><td>{html.escape(contract)}</td>"
            f"<td>{spec}</td><td>{links}</td>"
            "</tr>"
        )
    return (
        f"<h1>Zip services</h1><p>Signed in as <strong>{html.escape(user)}</strong>.</p>"
        '<p class="cta"><a class="button" href="/zip">Zip files in the browser</a></p>'
        '<p>Every service implements the same <a href="/docs/pages/contract">API contract</a>'
        " and is reached at <code>/&lt;service&gt;/api/v1/zip</code> with "
        "<code>Authorization: Bearer &lt;key&gt;</code>. Click an endpoint for its interactive "
        "reference, built from the service's own OpenAPI document where it publishes one; "
        'you can send requests to it from there. <a href="/docs/compare">Compare specs</a> '
        'shows where each document differs from the <a href="/docs/api">contract</a>.</p>'
        '<div class="scroll"><table><thead><tr><th>Endpoint</th><th>Status</th>'
        "<th>Latency</th><th>Build</th><th>Contract</th><th>Spec</th><th>Docs</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


async def _read_form(request: Request, limit: int = 4096) -> dict[str, str] | None:
    """A small urlencoded form, without python-multipart. None if it is too large."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")).items()}


def _keys_body(
    mine: Sequence[ApiKeyRecord],
    csrf: str,
    new_key: str | None,
    error: str | None,
    usage: tuple[int, int] | None = None,
) -> str:
    token = html.escape(csrf)
    parts = [
        "<h1>My API keys</h1>"
        "<p>Use a key from scripts and tools: "
        "<code>Authorization: Bearer &lt;key&gt;</code> on any "
        "<code>/&lt;service&gt;/api/v1/zip</code> request. Keys are stored only as a hash: "
        "if you lose one, revoke it and create another.</p>"
    ]
    if usage is not None:
        used, limit = usage
        parts.append(
            f'<p class="usage">Today (UTC): <strong>{human_bytes(used)}</strong> of your '
            f"{human_bytes(limit)} daily allowance, uploads plus archives, across your keys "
            "and the browser page. It resets at 00:00 UTC.</p>"
        )
    if new_key:
        parts.append(
            '<div class="new-key" role="status"><p><strong>Your new key.</strong> Copy it now: '
            "it is shown only once.</p>"
            f"<pre><code>{html.escape(new_key)}</code></pre></div>"
        )
    if error:
        parts.append(f'<p class="error" role="alert">{html.escape(error)}</p>')
    parts.append(
        '<form method="post" action="/account/keys" class="inline-form">'
        f'<input type="hidden" name="csrf" value="{token}">'
        '<label>Name <input name="name" maxlength="64" required '
        'placeholder="e.g. laptop scripts"></label><button>Create key</button></form>'
    )
    if not mine:
        parts.append('<p class="muted">You have no keys yet.</p>')
        return "".join(parts)
    rows = []
    for key in mine:
        status = "active" if key.active else f"revoked {pages.when(key.revoked)}"
        action = (
            f'<form method="post" action="/account/keys/{html.escape(key.prefix)}/revoke">'
            f'<input type="hidden" name="csrf" value="{token}"><button>Revoke</button></form>'
            if key.active
            else ""
        )
        rows.append(
            f"<tr><td>{html.escape(key.name)}</td>"
            f"<td><code>zk_{html.escape(key.prefix)}_...</code></td>"
            f"<td>{pages.when(key.created)}</td><td>{pages.when(key.last_used)}</td>"
            f"<td>{key.use_count}</td>"
            f'<td class="{"ok" if key.active else "muted"}">{html.escape(status)}</td>'
            f"<td>{action}</td></tr>"
        )
    parts.append(
        '<div class="scroll"><table><thead><tr><th>Name</th><th>Key</th><th>Created</th>'
        "<th>Last used</th><th>Requests</th><th>Status</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="muted">Usage is recorded about once a minute.</p>'
    )
    return "".join(parts)


def _zip_body(
    upstreams: Sequence[Upstream],
    statuses: Sequence[tuple[str, float | None]],
    all_docs: Sequence[ServiceDocs],
    csrf: str,
) -> str:
    """The upload page. zip.js drives it; without JavaScript it explains the API instead."""
    options = []
    first_ready = True
    for upstream, (state, _), service_docs in zip(upstreams, statuses, all_docs, strict=True):
        name = html.escape(upstream.name)
        ready = state == "ready"
        checked = " checked" if ready and first_ready else ""
        first_ready = first_ready and not ready
        version = html.escape(_build_label(service_docs).strip(" ()").split(" ")[0])
        options.append(
            f'<label class="service{"" if ready else " unavailable"}">'
            f'<input type="radio" name="service" value="{name}"{checked}'
            f"{'' if ready else ' disabled'}>"
            f'<span class="name">{name}</span>'
            f'<span class="{"ok" if ready else "bad"}">{html.escape(state)}</span>'
            f'<span class="muted">{version}</span></label>'
        )
    return (
        "<h1>Zip files</h1>"
        "<p>Choose a service, add files, and zip them. The archive downloads as it is "
        "made, under the service's default name; your browser decides where to save it. "
        "There is no size limit.</p>"
        f'<form id="zip-form" class="zip" data-csrf="{html.escape(csrf)}" novalidate>'
        '<fieldset class="services"><legend>Service</legend>'
        f"{''.join(options)}</fieldset>"
        '<div id="drop" class="drop">'
        '<input type="file" id="files" name="files" multiple class="visually-hidden">'
        '<label for="files"><strong>Drop files here</strong> or '
        '<span class="link">choose files</span></label></div>'
        '<ul id="file-list" class="file-list"></ul>'
        '<p id="total" class="muted"></p>'
        '<button id="zip-button" type="submit" disabled>Zip files</button>'
        '<progress id="progress" max="100" value="0" hidden></progress>'
        '<p id="status" class="status" role="status" aria-live="polite"></p>'
        "</form>"
        '<iframe id="download" name="download" title="Archive download" hidden></iframe>'
        "<noscript><p>This page needs JavaScript. Without it, use the API: see the "
        '<a href="/docs/api">API reference</a>.</p></noscript>'
        '<script src="/static/zip.js"></script>'
    )


_PORTAL_CSS = """
:root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fff; --muted: #6e6e73;
  --line: #d2d2d7; --accent: #0a66c2; --ok: #1a7f37; --bad: #c62828; }
@media (prefers-color-scheme: dark) { :root { --fg: #f5f5f7; --bg: #161618; --muted: #a1a1a6;
  --line: #3a3a3c; --accent: #4ea1ff; --ok: #3fb950; --bad: #ff6b6b; } }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.55 system-ui, sans-serif;
  color: var(--fg); background: var(--bg); }
header { display: flex; gap: 1.5rem; align-items: center; padding: .75rem 1rem;
  border-bottom: 1px solid var(--line); }
header .brand { font-weight: 700; margin-right: auto; }
header form { margin: 0; }
a { color: var(--accent); }
.layout { display: grid; grid-template-columns: 14rem minmax(0, 1fr);
  max-width: 72rem; margin: 0 auto; }
nav { padding: 1rem; border-right: 1px solid var(--line); }
nav h3 { font-size: .8rem; text-transform: uppercase; color: var(--muted);
  margin: 1rem 0 .25rem; }
nav ul { list-style: none; padding: 0; margin: 0; }
main { padding: 1rem 2rem; min-width: 0; }
pre { overflow-x: auto; padding: .75rem; border: 1px solid var(--line); border-radius: 6px; }
code { font-size: .9em; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line); }
.ok { color: var(--ok); } .bad { color: var(--bad); } .error { color: var(--bad); }
.crumb, .muted { color: var(--muted); }
.scroll { overflow-x: auto; }
.login { display: flex; flex-direction: column; gap: .75rem; max-width: 24rem; }
.login input { width: 100%; padding: .5rem; font: inherit; }
details summary { cursor: pointer; color: var(--muted); margin: 1.5rem 0 .75rem; }
td form { margin: 0; }
button { font: inherit; padding: .4rem .9rem; cursor: pointer; }
@media (max-width: 720px) { .layout { grid-template-columns: 1fr; }
  nav { border-right: 0; border-bottom: 1px solid var(--line); } main { padding: 1rem; } }
.button { display: inline-block; padding: .5rem 1rem; border-radius: 6px;
  background: var(--accent); color: var(--bg); text-decoration: none; font-weight: 600; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; }
.zip { display: flex; flex-direction: column; gap: 1rem; max-width: 40rem; }
.services { display: flex; flex-wrap: wrap; gap: .5rem; border: 0; padding: 0; margin: 0; }
.services legend { font-weight: 600; margin-bottom: .5rem; }
.service { display: flex; gap: .5rem; align-items: center; padding: .5rem .75rem;
  border: 1px solid var(--line); border-radius: 6px; cursor: pointer; }
.service:has(input:checked) { border-color: var(--accent); }
.service.unavailable { opacity: .55; cursor: not-allowed; }
.service .name { font-weight: 600; }
.drop { border: 2px dashed var(--line); border-radius: 8px; padding: 2rem 1rem;
  text-align: center; }
.drop label { cursor: pointer; display: block; }
.drop .link { color: var(--accent); text-decoration: underline; }
.drop.over { border-color: var(--accent); }
.drop:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.file-list { list-style: none; padding: 0; margin: 0; }
.file-list li { display: flex; gap: .75rem; align-items: center; padding: .3rem 0;
  border-bottom: 1px solid var(--line); }
.file-list .file-name { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.file-list button { padding: .1rem .5rem; }
.zip > button { align-self: flex-start; }
progress { width: 100%; }
.status.error { color: var(--bad); } .status.done { color: var(--ok); }
.inline-form { display: flex; flex-wrap: wrap; gap: .5rem; align-items: end; margin: 1rem 0; }
.inline-form input { padding: .4rem; font: inherit; }
td form { margin: 0; }
.new-key { border: 1px solid var(--ok); border-radius: 6px; padding: .5rem 1rem; margin: 1rem 0; }
.new-key pre { user-select: all; }
"""

# The upload page (/zip): select files, then run one job (browser_jobs.py):
#   1. POST /zip/jobs                 -> job id and its two URLs
#   2. hidden iframe GET .../archive       -> the browser's own download, streamed to disk
#   3. XHR POST .../files (FormData)       -> upload progress, then the JSON outcome
# No framework and no build step; everything shown is set with textContent.
_ZIP_JS = r"""
(() => {
  'use strict';
  const form = document.getElementById('zip-form');
  const csrf = form.dataset.csrf;
  const drop = document.getElementById('drop');
  const input = document.getElementById('files');
  const list = document.getElementById('file-list');
  const total = document.getElementById('total');
  const button = document.getElementById('zip-button');
  const progress = document.getElementById('progress');
  const status = document.getElementById('status');
  const frame = document.getElementById('download');
  let files = [];
  let busy = false;

  const size = (bytes) => {
    const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit ? value.toFixed(1) : value) + ' ' + units[unit];
  };

  const say = (text, kind) => {
    status.textContent = text;
    status.className = 'status' + (kind ? ' ' + kind : '');
  };

  const service = () => {
    const chosen = form.querySelector('input[name="service"]:checked');
    return chosen ? chosen.value : null;
  };

  const render = () => {
    list.replaceChildren(...files.map((file, index) => {
      const item = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = file.name;
      const bytes = document.createElement('span');
      bytes.className = 'muted';
      bytes.textContent = size(file.size);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = 'Remove';
      remove.setAttribute('aria-label', 'Remove ' + file.name);
      remove.disabled = busy;
      remove.addEventListener('click', () => { files.splice(index, 1); render(); });
      item.append(name, bytes, remove);
      return item;
    }));
    const sum = files.reduce((acc, file) => acc + file.size, 0);
    total.textContent = files.length
      ? files.length + (files.length === 1 ? ' file, ' : ' files, ') + size(sum)
      : 'No files yet.';
    button.disabled = busy || !files.length || !service();
  };

  const add = (fileList) => {
    for (const file of fileList) files.push(file);
    render();
  };

  input.addEventListener('change', () => { add(input.files); input.value = ''; });
  for (const name of ['dragenter', 'dragover']) {
    drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add('over'); });
  }
  for (const name of ['dragleave', 'drop']) {
    drop.addEventListener(name, () => drop.classList.remove('over'));
  }
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    if (!busy && event.dataTransfer) add(event.dataTransfer.files);
  });
  form.addEventListener('change', render);

  const problemText = (problem, fallback) =>
    problem && (problem.detail || problem.title)
      ? (problem.title ? problem.title + ': ' : '') + (problem.detail || '')
      : fallback;

  const finish = (text, kind) => {
    busy = false;
    progress.hidden = true;
    say(text, kind);
    render();
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !files.length || !service()) return;
    busy = true;
    render();
    say('Starting…');

    let job;
    try {
      const response = await fetch('/zip/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ service: service() }),
        credentials: 'same-origin',
      });
      if (response.status === 401) { window.location.href = '/account/login?next=/zip'; return; }
      const body = await response.json().catch(() => null);
      if (!response.ok) { finish(problemText(body, 'Could not start the job.'), 'error'); return; }
      job = body;
    } catch (error) {
      finish('Could not reach the gateway.', 'error');
      return;
    }

    // The download half first: the browser saves the archive as it streams in.
    frame.src = job.download;

    const data = new FormData();
    for (const file of files) data.append('files', file, file.name);
    const request = new XMLHttpRequest();
    request.open('POST', job.upload);
    request.setRequestHeader('X-CSRF-Token', csrf);
    request.responseType = 'json';
    progress.value = 0;
    progress.hidden = false;
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) {
        progress.value = Math.round((event.loaded / event.total) * 100);
        say('Uploading and zipping… ' + size(event.loaded) + ' of ' + size(event.total));
      }
    });
    request.upload.addEventListener('load', () => say('Finishing the archive…'));
    request.addEventListener('load', () => {
      const result = request.response || {};
      if (request.status === 200 && result.outcome === 'completed') {
        files = [];
        finish('Done: ' + (result.filename || 'the archive') +
          ' (' + size(result.bytes_out || 0) + ') was saved by your browser.', 'done');
      } else if (result.outcome === 'rejected') {
        finish(problemText(result.problem, 'The service rejected the upload.'), 'error');
      } else if (result.outcome === 'cancelled') {
        finish('The download was cancelled, so the job stopped.', 'error');
      } else {
        finish(problemText(result, 'The archive failed part way (' +
          (result.code || 'unknown error') + '). Discard the partial download.'), 'error');
      }
    });
    request.addEventListener('error', () =>
      finish('The upload failed: the connection was lost.', 'error'));
    request.send(data);
  });

  render();
})();
"""

# Configuration only: the page itself carries no inline script (see the CSP above).
_API_REFERENCE_JS = """
const container = document.getElementById('api-reference');
Scalar.createApiReference('#api-reference', {
  url: container.dataset.spec, // prepared for the gateway by openapi_view.for_gateway
  withDefaultFonts: false,
  hideClientButton: false,
  // Generated snippets send an array of files as a JSON string; the gateway generates a
  // correct curl sample for every operation instead.
  hiddenClients: true,
  authentication: { preferredSecurityScheme: 'gatewayKey' },
});
"""

# The strip above Scalar on reference pages: where the document comes from, and how to
# switch to the other one.
_API_BAR_CSS = """
.spec-bar { display: flex; flex-wrap: wrap; gap: .25rem 1.25rem; align-items: center;
  padding: .6rem 1rem; font: 14px/1.45 system-ui, sans-serif; color: #1d1d1f;
  background: #f5f5f7; border-bottom: 1px solid #d2d2d7; }
.spec-bar a { color: #0a66c2; }
.spec-bar .source { font-weight: 600; }
@media (prefers-color-scheme: dark) {
  .spec-bar { color: #f5f5f7; background: #1c1c1e; border-color: #3a3a3c; }
  .spec-bar a { color: #4ea1ff; } }
"""

_STATIC: dict[str, tuple[str, str]] = {
    "portal.css": (_PORTAL_CSS, "text/css"),
    "api-reference.js": (_API_REFERENCE_JS, "text/javascript"),
    "api-bar.css": (_API_BAR_CSS, "text/css"),
    "zip.js": (_ZIP_JS, "text/javascript"),
}
