<!-- Contract 1.1.0, §14. Appended to PROMPT.md Part 2 by scripts/sync-contract.sh. The
     prompts given to the agents carry a copy of this text: keep them in step. -->
## 14. Documentation endpoints

These endpoints are **not** under `/api/v1`, are excluded from the concurrency limit,
and need no authentication: the service is only reachable from the private network, and
the gateway authenticates users.

### 14.1 Manifest

`GET /docs/manifest.json` → `200`, `Content-Type: application/json`

```json
{
  "service": "zip-service",
  "version": "1.1.0",
  "revision": "3f2c9e1",
  "contract_version": "1.1.0",
  "openapi": "/docs/openapi.json",
  "documents": [
    {"slug": "readme", "title": "README", "path": "/docs/README.md"},
    {"slug": "agents", "title": "AGENTS", "path": "/docs/AGENTS.md"}
  ]
}
```

- `version` is the service's own version (from `pyproject.toml`).
- `revision` is the git commit the image was built from, read from the `GIT_SHA`
  environment variable; `"unknown"` if it is unset. The Dockerfile MUST accept an
  optional `GIT_SHA` build argument and set it as an environment variable, and
  `docker build .` MUST still work without it.
- `documents` MUST include `readme` and `agents`, in that order, and MAY list more (at
  most 20). `slug` matches `^[a-z0-9-]{1,32}$`; `path` matches
  `^/docs/[A-Za-z0-9._-]{1,64}\.md$`; `title` is at most 80 characters.
- `openapi` is the path of the service's own OpenAPI document (§14.4). It SHOULD be
  present; if present it matches `^/docs/[A-Za-z0-9._-]{1,64}\.json$`.

### 14.2 Documents

`GET /docs/<name>.md` → `200`, `Content-Type: text/markdown; charset=utf-8`

- Every document in the manifest MUST be served, and nothing else.
- Documents are files baked into the image at build time. They are never generated per
  request and never read from outside the image. Each MUST be at most 1 MiB.
- An unknown name returns `404 not-found` as Problem Details, like any other unknown path.
- Responses include `Cache-Control: no-cache` and `X-Content-Type-Options: nosniff`.

### 14.3 Markdown the gateway renders

The gateway renders CommonMark plus GitHub-style tables and strikethrough. Raw HTML is
shown as escaped text, so do not use it. Mermaid and other diagram blocks are shown as
plain code. Relative links between your own documents do not resolve in the portal, so
refer to other documents by name.

### 14.4 OpenAPI document

`GET /docs/openapi.json` (the path in the manifest) → `200`, `Content-Type:
application/json`: an OpenAPI 3.x document describing this service.

- It describes **what this implementation actually does**, generated from the code
  wherever the framework allows. Where it differs from the contract, document the
  implementation, not the contract: the portal compares the two and reports the
  differences.
- It describes the service as reached directly: no `servers` and no `security`. The
  gateway adds its URL, its API-key authentication and its own errors when it displays
  the document.
- It MUST be at most 1 MiB and MUST NOT require network access to produce.
