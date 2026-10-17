"""The gateway's public origin, as the browser sees it.

Behind Caddy or the AWS load balancer, `X-Forwarded-Proto` (and `X-Forwarded-Host`, or the
`Host` header) carry the public scheme and host. Used for the server URL in the OpenAPI
documents shown to signed-in users, and to check the `Origin` of state-changing browser
requests. Anything that is not a plain host[:port] is rejected rather than reflected.
"""

import re

from starlette.datastructures import Headers

_HOST = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")


def public_origin(headers: Headers, scheme: str) -> str:
    """`https://zip.example.com`, or "" if the headers do not give a usable origin."""
    proto = headers.get("x-forwarded-proto", scheme).split(",")[0].strip().lower()
    host = (headers.get("x-forwarded-host") or headers.get("host", "")).split(",")[0].strip()
    if proto not in ("http", "https") or not _HOST.fullmatch(host):
        return ""
    return f"{proto}://{host}"


def same_origin(headers: Headers, scheme: str) -> bool:
    """True if the request's `Origin` header is this gateway's public origin.

    Browsers send `Origin` on every cross-site request and on same-origin POSTs, so a
    missing header on a state-changing request is treated as a failure.
    """
    origin = headers.get("origin")
    expected = public_origin(headers, scheme)
    return bool(origin and expected and origin == expected)
