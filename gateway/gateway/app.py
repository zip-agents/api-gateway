"""Top-level ASGI app: request IDs, then dispatch on the first path segment.

* `/<service>/...` for a configured service goes to the streaming proxy (proxy.py), which
  is plain ASGI so that nothing in a framework can buffer the body.
* Everything else (`/health/*`, `/docs/*`, `/`) is a small Starlette app (portal.py).
"""

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.accounts.authn import Authenticator
from gateway.accounts.service import Accounts
from gateway.accounts.store import SqlStore
from gateway.auth import KeyRing, SessionSigner
from gateway.browser_jobs import BrowserJobs, JobRegistry
from gateway.config import Settings
from gateway.docs import DocsSite
from gateway.logs import request_id_var
from gateway.portal import create_portal
from gateway.proxy import ServiceProxy
from gateway.service_docs import ServiceDocsCache

# Same rule as the zip services (contract §10), so an ID survives both hops unchanged.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            text = value.decode("latin-1")
            if _REQUEST_ID.fullmatch(text):
                return str(text)
            break
    return str(uuid.uuid4())


class Gateway:
    def __init__(
        self,
        settings: Settings,
        docs: DocsSite | None = None,
        accounts: Accounts | None = None,  # tests pass fakes for Cognito
    ) -> None:
        keyring = KeyRing(settings.api_keys)
        sessions = SessionSigner(settings.session_secret, settings.session_ttl_seconds, keyring)
        if accounts is None:
            accounts = Accounts.build(settings, SqlStore.from_url(settings.database_url))
        store = accounts.store
        keys = accounts.keys

        @asynccontextmanager
        async def lifespan(_: object) -> AsyncIterator[None]:
            # Migrate before serving, so a broken database fails the deploy, not a request.
            await store.ensure_schema()
            try:
                yield
            finally:
                await keys.flush()  # usage counted since the last flush
                await accounts.quota.flush()
                await store.close()

        self._settings = settings
        self._proxy = ServiceProxy(settings, Authenticator(keyring, keys), accounts.quota)
        jobs = BrowserJobs(
            settings,
            sessions,
            self._proxy,
            JobRegistry(settings.browser_jobs_max),
            accounts.users,
            accounts.quota,
        )
        self._portal: ASGIApp = create_portal(
            settings,
            keyring,
            sessions,
            docs or DocsSite(),
            ServiceDocsCache(settings.upstreams),
            jobs,
            accounts,
            lifespan,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._portal(scope, receive, send)  # lifespan
            return
        request_id = incoming_request_id(scope)
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "x-request-id" not in headers:
                    headers["X-Request-ID"] = request_id
            await send(message)

        try:
            segment = scope["path"].split("/", 2)[1]
            upstream = self._settings.upstreams.get(segment)
            if upstream is not None:
                await self._proxy(scope, receive, send_with_id, upstream, request_id)
            else:
                await self._portal(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
