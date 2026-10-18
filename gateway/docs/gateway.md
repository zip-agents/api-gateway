# Using the gateway

Three independent implementations of the same [API contract](/docs/pages/contract) run
behind this gateway, one container each. The first path segment picks the implementation:

| Service | Endpoint | Built by |
|---|---|---|
| `claude` | `/claude/api/v1/zip` | Claude Code (Opus 5.5) |
| `grok` | `/grok/api/v1/zip` | Grok Build (4.7) |
| `dsh` | `/dsh/api/v1/zip` | DeepSeek harness, local Qwen 3.8 |

Everything after the prefix is passed to the service unchanged, so
`/claude/health/ready` is that service's readiness check.

## Zip files in the browser

Signed in, open [Zip files](/zip): choose a service, drop or choose files, and the
archive downloads under the service's default name as it is made. There is no size
limit: the page streams the upload and the download at the same time.

## Authentication

Send your API key as a bearer token on every request:

```bash
curl -sS -o out.zip -D - \
  -H "Authorization: Bearer $API_KEY" \
  -F 'filename=quarterly report' \
  -F 'files=@a.txt' -F 'files=@photo.jpg' \
  https://<gateway host>/claude/api/v1/zip
```

The gateway strips the `Authorization` header before forwarding: the services never see
your key. A missing or wrong key gets `401` with
`urn:zip-gateway:problem:unauthorized`.

These docs use a separate browser session: signing in with your key sets a cookie that is
only valid under `/docs`. The cookie is never accepted by the API routes.

## Streaming through the gateway

The gateway streams in both directions and buffers at most one chunk each way, so the
contract's streaming behaviour is preserved end to end:

- Archive bytes start arriving while you are still uploading, provided your client reads
  the response concurrently (full duplex). `curl` does.
- Clients that only read after they finish uploading (half duplex) work when the
  compressed output produced during the upload fits in the service's output buffer
  (16 MiB by default). See contract §4.1.
- If a service fails mid-stream, the gateway closes your connection without the final
  chunk, exactly as the service would. A truncated archive always means failure.

## Errors from the gateway itself

| Status | `code` | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing or invalid API key |
| 404 | `not-found` | Unknown service or path |
| 502 | `upstream-unavailable` | The service could not be reached (not deployed, restarting) |
| 502 | `upstream-error` | The service closed the connection before sending a response |
| 504 | `upstream-timeout` | No bytes moved in either direction for 60 seconds |

Every response carries `X-Request-ID`. Send your own (`^[A-Za-z0-9._-]{1,128}$`) and it is
passed to the service, so one ID ties together the gateway's and the service's logs.
