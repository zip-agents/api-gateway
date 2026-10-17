> Assembled by scripts/sync-contract.sh from PROMPT.md (Part 2) and
> contract/14-documentation.md (§14). Edit those, not this copy.

# Part 2: API and behaviour contract

**Contract version:** 1.0.0

This part is the authoritative specification for the service. A black-box conformance test suite will be run against the built container, so the behaviour described here must be matched **exactly**. Where this contract is silent, the implementation is free to choose, but must document its choice in the README.

The key words MUST, MUST NOT, SHOULD and MAY are used as described in RFC 2119.

---

## 1. Overview

The service accepts one or more files in a single `multipart/form-data` request and returns a ZIP archive containing them. The archive is **streamed**: the service compresses each file as its bytes arrive and sends output as it is produced. Upload data MUST NOT be written to disk, and memory use per request MUST be bounded by configuration, independent of upload size.

---

## 2. Endpoint

```
POST /api/v1/zip
Content-Type: multipart/form-data; boundary=...
```

### 2.1 Request format

| Condition | Result |
|---|---|
| `Content-Type` is not `multipart/form-data` (case-insensitive) | `415 unsupported-media-type` |
| `Content-Type` is multipart but has no `boundary` parameter | `400 malformed-request` |
| Body is not valid multipart | `400 malformed-request` if detected before streaming begins (§5); otherwise abort (§6) |

### 2.2 Parts

A **file part** is any part whose `Content-Disposition` header has a `filename` parameter. Every other part is a **field part**.

**`filename` field (optional).** This is the desired archive name.
- It MUST appear before the first file part to take effect.
- Field parts received after the first file part are ignored and logged at `WARNING`.
- If more than one `filename` field appears before the first file part, return `422 invalid-archive-name`.
- A field value over 1024 bytes returns `422 invalid-archive-name`.

**`files` (required, one or more).** These are the files to archive.
- File parts MUST use the field name `files`. A file part with any other field name returns `400 unexpected-file-field` if it is the first file part. If it appears later, it is a mid-stream error (§6).
- A file part whose `filename` parameter is empty (`filename=""`) is **skipped**. It does not count as a file and is not added to the archive. This is how browsers submit an empty file input.
- Zero-byte files with a non-empty filename are valid and are included.

**Other field parts** before the first file part are ignored.

If the body ends without any non-skipped file part, return `400 no-files`.

### 2.3 Archive name rules (the `filename` field)

These rules are applied in order:

1. Absent, or empty after trimming leading and trailing whitespace → use `zipped_files.zip`.
2. Contains any control character (U+0000–U+001F or U+007F), `/` or `\` → `422 invalid-archive-name`.
3. Normalise to Unicode NFC.
4. If the name does not end in `.zip` (case-insensitive), **append `.zip`**. For example, `report` becomes `report.zip`, and `report.ZIP` stays unchanged.
5. If the result is longer than 255 bytes in UTF-8 → `422 invalid-archive-name`.

### 2.4 Entry name rules (each file in the archive)

Entry names are **sanitised, never rejected**. Later file parts arrive after streaming has begun, so rejection is not possible for them. These rules are applied in order:

1. Take the part's `filename` parameter. Where both `filename` and `filename*` are present, prefer `filename*`.
2. Replace `\` with `/`, then keep only the final path segment. The archive is always **flat**.
3. Remove control characters (U+0000–U+001F and U+007F).
4. Trim leading and trailing whitespace, and normalise to Unicode NFC.
5. If the result is empty, `.` or `..`, use `file`.
6. If the result is longer than 255 bytes in UTF-8, truncate it on a character boundary:
   - If it has an extension of 16 bytes or fewer, preserve the extension and truncate the stem.
   - Otherwise truncate the whole name.
7. **De-duplicate.** Names are compared case-insensitively after Unicode case folding. On a collision, insert ` (n)` before the extension, using the lowest `n ≥ 1` that gives a unique name:
   - `report.txt` → `report (1).txt` → `report (2).txt`
   - `README` → `README (1)`
   - `archive.tar.gz` → `archive.tar (1).gz`. The extension is everything after the **last** dot.
   - `.env` → `.env (1)`. A leading dot does not start an extension.
   - Generated names are themselves checked for collisions. For example, uploading `a.txt`, `a (1).txt`, `a.txt` yields `a.txt`, `a (1).txt`, `a (2).txt`.

Other entry properties:
- Entries appear in the archive in upload order.
- The UTF-8 flag (general purpose bit 11) MUST be set for entry names containing non-ASCII characters.
- All entries share one modification timestamp: the time the request was received, in UTC.

### 2.5 Archive format

- **Compression.** DEFLATE at `ZIP_COMPRESSION_LEVEL` (default `6`). Level `0` means the STORED method, not DEFLATE level 0.
- **Zip64.** Every entry MUST be written with Zip64 extensions, so there is no 4 GiB limit on entries or on the archive. Some older unzip tools handle Zip64 archives poorly, so the README MUST state which tools the output was tested with.
- **Validity.** The archive MUST pass `unzip -t`, `7z t`, and Python `zipfile.ZipFile.testzip()`.

---

## 3. Successful response

```
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename="<ascii-fallback>"; filename*=UTF-8''<percent-encoded>
Cache-Control: no-store
X-Content-Type-Options: nosniff
X-Request-ID: <request id>
Transfer-Encoding: chunked
```

- **No `Content-Length`.** The response size is not known in advance.
- **ASCII fallback for the filename.** Replace every non-ASCII character, `"` and `\` with `_`.
- **The `filename*` value.** This is the UTF-8 archive name, percent-encoded per RFC 5987/RFC 8187.

---

## 4. Limits

All limits are read from environment variables (§7).

| Limit | Variable | Default |
|---|---|---|
| Maximum number of file parts (excluding skipped parts) | `MAX_FILES` | `500` |
| Maximum concurrent in-flight zip requests per process | `MAX_CONCURRENT_REQUESTS` | `32` |
| Maximum time with no progress in either direction | `IDLE_TIMEOUT_SECONDS` | `30` |
| Maximum total request duration | `MAX_REQUEST_DURATION_SECONDS` | `900` |
| Maximum compressed output buffered while the client is not reading | `OUTPUT_BUFFER_MAX_BYTES` | `16777216` (16 MiB) |

How limits are enforced:
- **No size limits.** There is no limit on the size of any file or on the total upload. Upload size is bounded only by `MAX_REQUEST_DURATION_SECONDS` and throughput. `Content-Length` is not required, and is not used for limits.
- **Concurrency.** A request over the concurrency limit gets `503 overloaded` immediately, with a `Retry-After: 5` header.
- **Timeouts.** "No progress" means no request bytes were read and no response bytes were sent. A request that exceeds either timeout gets `408 request-timeout` if streaming has not begun; otherwise it is aborted (§6).

| Limit exceeded | Before streaming begins | After streaming begins |
|---|---|---|
| `MAX_FILES` | `413 too-many-files` | abort |

### 4.1 Client duplex behaviour and the output buffer

Many HTTP clients don't read the response until they have finished sending the request. If the service keeps writing output that the client isn't reading, both sides block forever.

To prevent this, the service buffers compressed output in memory, up to `OUTPUT_BUFFER_MAX_BYTES`. When the buffer is full, the service stops reading the request until the client drains output. If no progress happens in either direction for `IDLE_TIMEOUT_SECONDS`, the request is aborted.

As a consequence:
- **Half-duplex clients** succeed whenever the output produced before the upload finishes fits in the buffer.
- **Full-duplex clients** succeed at any size within the limits.

This behaviour MUST be documented in the README.

---

## 5. When streaming begins

**Streaming begins** at the moment the service sends the response status line and headers. This MUST happen no earlier than the start of the first non-skipped file part, and only after the archive name has been validated.

Up to that point, every error is returned as a normal HTTP error response (§6.1).

---

## 6. Errors

### 6.1 Error responses (before streaming begins)

All error responses, including framework-generated `404` and `405`, use RFC 9457 Problem Details:

```
Content-Type: application/problem+json
X-Request-ID: <request id>
```

```json
{
  "type": "urn:zip-service:problem:no-files",
  "title": "No files were uploaded",
  "status": 400,
  "detail": "The request contained no file parts with a non-empty filename.",
  "code": "no-files",
  "request_id": "3f0c..."
}
```

| Status | `code` | When |
|---|---|---|
| 400 | `malformed-request` | Missing boundary, invalid multipart before streaming |
| 400 | `no-files` | No non-skipped file parts |
| 400 | `unexpected-file-field` | First file part uses a field name other than `files` |
| 404 | `not-found` | Unknown path |
| 405 | `method-not-allowed` | Wrong method for a known path |
| 408 | `request-timeout` | Idle or total-duration timeout before streaming |
| 413 | `too-many-files` | See §4 |
| 415 | `unsupported-media-type` | Not `multipart/form-data` |
| 422 | `invalid-archive-name` | See §2.2 and §2.3 |
| 500 | `internal-error` | Unexpected failure. The response MUST NOT contain stack traces or internal details. |
| 503 | `overloaded` | Concurrency limit reached (with `Retry-After`) |
| 503 | `shutting-down` | New request received during graceful shutdown |

### 6.2 Mid-stream failures (after streaming begins)

After the status line has been sent, the service cannot report errors through the HTTP status. On any failure (limit exceeded, malformed multipart, timeout, internal error), the service MUST:

1. Stop sending immediately. It MUST NOT write the end-of-central-directory record or the terminating zero-length chunk.
2. Close the connection. The client then receives a truncated archive that unzip tools will reject.
3. Log the failure at `ERROR` (`WARNING` for client-caused failures), with the request ID and the `code` it would have used.

If the client disconnects, the service MUST stop work promptly and release all resources.

---

## 7. Configuration

All configuration comes from environment variables and is validated at startup. Invalid configuration MUST cause the process to exit with a non-zero status and a clear message.

**Development: the `.env` file.**
- In development, settings MUST also be loaded from a `.env` file in the project root, if one exists.
- Real environment variables MUST take precedence over values in `.env`.
- If neither sets a variable, the default below applies, so the service runs with no `.env` file at all.
- `.env` MUST be listed in `.gitignore` and `.dockerignore`. It is never committed and never baked into the image.
- A committed `.env.example` MUST list every variable below, with its default and a one-line description.

In every other environment, values are supplied as real environment variables.

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `8000` | Listens on `0.0.0.0` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `ZIP_COMPRESSION_LEVEL` | `6` | `0`–`9`. `0` means the STORED method. |
| `SHUTDOWN_GRACE_SECONDS` | `25` | See §9 |
| All limits in §4 | as listed | Positive integers |

---

## 8. Health endpoints

These endpoints are **not** under `/api/v1`, and they are excluded from the concurrency limit.

| Endpoint | Response |
|---|---|
| `GET /health/live` | `200 {"status": "ok"}` whenever the process is running |
| `GET /health/ready` | `200 {"status": "ready"}` when accepting work; `503 {"status": "shutting-down"}` during shutdown |

---

## 9. Lifecycle

On `SIGTERM`, the service MUST:
1. Start failing readiness checks.
2. Reject new zip requests with `503 shutting-down`.
3. Allow in-flight requests up to `SHUTDOWN_GRACE_SECONDS` to finish, then abort any that remain and exit.

---

## 10. Observability

**Request ID.**
- If the incoming request has an `X-Request-ID` header matching `^[A-Za-z0-9._-]{1,128}$`, use it.
- Otherwise generate a UUIDv4.
- Echo it on every response, and include it in every log line for that request.

**Logs.**
- JSON lines to stdout, one object per line. Nothing else may be written to stdout.
- Every line includes `timestamp` (RFC 3339, UTC), `level`, `message`, and `request_id` where applicable.
- Each zip request emits exactly one completion log line with these additional fields:

| Field | Meaning |
|---|---|
| `method`, `path`, `status` | Request method, path, and HTTP status sent (or would-have-sent for aborts) |
| `duration_ms` | Total request time |
| `bytes_in` | Request body bytes read |
| `bytes_out` | Response body bytes sent |
| `files_count` | Non-skipped file parts processed |
| `outcome` | One of `completed`, `rejected`, `aborted`, `client_disconnected` |
| `code` | Error code, for non-`completed` outcomes |

---

## 11. Container requirements

- The image MUST build with `docker build .` from the service's root directory, with no build arguments required.
- It MUST run as a non-root user.
- It MUST work with a read-only root filesystem.
- It MUST listen on `PORT`.
- It MUST NOT require any network access at runtime.

---

## 12. Non-goals

The following are out of scope for this service:
- authentication and authorisation
- TLS termination
- per-client rate limiting
- persistence of uploads or archives
- directory structure inside archives (archives are always flat, see §2.4)

---

## 13. Example

```bash
curl -sS -o out.zip -D - \
  -F 'filename=quarterly report' \
  -F 'files=@a.txt' \
  -F 'files=@a.txt' \
  -F 'files=@photo.jpg' \
  http://localhost:8000/api/v1/zip
```

Response headers include:

```
Content-Disposition: attachment; filename="quarterly report.zip"; filename*=UTF-8''quarterly%20report.zip
```

The archive contains `a.txt`, `a (1).txt` and `photo.jpg`.

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
