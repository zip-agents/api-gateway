"""RFC 9457 Problem Details for errors the gateway itself produces.

Errors produced by a zip service are passed through untouched, so clients see the
service's own `urn:zip-service:problem:*` types. The gateway's types use a separate
namespace so a client can tell which hop failed.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.responses import Response

PROBLEM_CONTENT_TYPE = "application/problem+json"


@dataclass(frozen=True)
class Problem:
    status: int
    title: str
    detail: str


PROBLEMS: dict[str, Problem] = {
    "unauthorized": Problem(
        401, "Authentication required", "Send a valid API key as 'Authorization: Bearer <key>'."
    ),
    "forbidden": Problem(
        403, "Forbidden", "The request failed the cross-site request checks (Origin, CSRF token)."
    ),
    "not-found": Problem(404, "Not found", "No route matches this path."),
    "method-not-allowed": Problem(
        405, "Method not allowed", "This path does not support the request method."
    ),
    "request-timeout": Problem(
        408, "Request timeout", "The other half of the upload job did not arrive in time."
    ),
    "quota-exceeded": Problem(
        429, "Daily quota used up", "This account has used its daily allowance of bytes."
    ),
    "overloaded": Problem(
        503, "Too many upload jobs", "Too many upload jobs are in progress. Retry shortly."
    ),
    "upstream-unavailable": Problem(
        502, "Service unavailable", "The zip service for this route could not be reached."
    ),
    "upstream-error": Problem(
        502, "Bad gateway", "The zip service closed the connection without a valid response."
    ),
    "upstream-timeout": Problem(
        504, "Gateway timeout", "The zip service made no progress within the gateway's timeout."
    ),
    "internal-error": Problem(500, "Internal error", "The gateway failed unexpectedly."),
}


def problem_body(code: str, request_id: str, detail: str | None = None) -> bytes:
    problem = PROBLEMS[code]
    return json.dumps(
        {
            "type": f"urn:zip-gateway:problem:{code}",
            "title": problem.title,
            "status": problem.status,
            "detail": detail or problem.detail,
            "code": code,
            "request_id": request_id,
        },
        separators=(",", ":"),
    ).encode()


def problem_response(
    code: str,
    request_id: str,
    *,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return Response(
        problem_body(code, request_id, detail),
        status_code=PROBLEMS[code].status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store", **(headers or {})},
    )
