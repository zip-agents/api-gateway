# zip-gateway

An authenticating, streaming reverse proxy in front of the three zip service
implementations, plus an authenticated docs portal. `/<service>/<path>` is forwarded to
`<path>` on that service, so `/claude/api/v1/zip` reaches claude's `/api/v1/zip`.

It lives in the zip-platform repository, next to the infrastructure it deploys onto: the
zip services are separate repositories, the gateway and the platform change together.

## Run and test

Only Docker is needed. Tests never run on the host: they run in this Dockerfile's `dev`
stage, which holds the locked dev dependencies and a copy of the code.

```bash
# from gateway/
make lock           # first time only: resolve uv.lock
make test           # unit tests        (make test PYTEST_ARGS="-k proxy -x")
make check          # ruff, format check, mypy, tests with coverage: what CI runs
make fmt            # apply ruff formatting

# from the repository root
make env            # .env with generated local secrets; prints your API key
make up             # gateway + claude + grok on http://localhost:8080
make smoke          # smoke tests for each service, through the gateway
make test           # all of the above checks, then the stack and smoke tests
```

Then open <http://localhost:8080/docs> and sign in with the key from `.env`.

## Design

| Decision | Why |
|---|---|
| Proxy written on h11 and raw ASGI, not httpx/aiohttp | The services stream full-duplex (contract §4.1). httpx finishes sending the request before it reads the response, which would cap every upload at the service's 16 MiB output buffer. With h11 the gateway pumps the two directions independently. |
| Nothing buffered beyond one chunk each way | Memory per request stays bounded, and backpressure flows through: a slow client slows the service, and the service's idle timeout still applies. |
| Mid-stream failures are passed on as truncation | If a service aborts after `200`, the gateway closes the client connection without the terminating chunk, so the client sees what contract §6.2 promises. |
| `Authorization: Bearer` for the API, a `/docs`-scoped cookie for the portal | Browsers cannot attach headers to page loads, and the API never accepts the cookie, so a signed-in browser cannot be made to upload (no CSRF). |
| Named API keys (`name:secret`) compared by SHA-256 digest | Logs record who called (the name) but never the key, and every key is checked in constant time. |
| One upstream connection per request, no pooling | Requests are long streams, so the connection set-up cost is negligible. |
| Gateway errors use `urn:zip-gateway:problem:*` | Clients can tell whether the gateway or the service failed. Service errors pass through untouched. |

## Docs portal

- **Two kinds of OpenAPI document.** `docs/openapi.yaml` is the contract, written by
  hand. Each service may also publish its own (contract §14.4), generated from its code
  and describing what it actually does. Both describe a service as reached directly.
- `/docs/api/<service>` is an interactive reference (Scalar) for the service's own
  document, or for the contract if it publishes none; `?source=contract` switches. Every
  endpoint on the home page links there, and you can send real requests from it.
  `/docs/api` is the contract with every service in its server list.
- Before display, `openapi_view.for_gateway` rewrites a copy of either document the same
  way: the server becomes the public gateway URL (from `X-Forwarded-Proto`/`-Host`
  behind Caddy), the gateway's API key becomes the only security scheme, the gateway's
  own 401/429/502/504 are added, and every operation gets a generated curl sample. Scalar's
  own snippets are hidden: they send an array of files as a JSON string.
- `/docs/compare` lists where each service's document differs from the contract:
  operations, response statuses, request media types and fields, required fields and
  success media types. Differences are findings for the comparison, not errors.
- Scalar is pinned with an SRI hash, and the CSP's `connect-src 'self'` keeps "try it"
  requests (and the key typed into them) on this origin.
- `/docs/pages/contract` is the contract. It is assembled by `scripts/sync-contract.sh`,
  and CI fails if it is stale.
- Each service's own docs are fetched at runtime from the service (contract §14,
  `/docs/manifest.json`). They are cached for 5 minutes, or 30 seconds after a failure,
  and rendered with raw HTML escaped. A service without §14 shows as "docs unavailable".
- `/docs` lists each service's readiness, latency, version and git revision.

## Zip files in the browser

`/docs/zip` (signed in) lets a user pick a service, drop or choose files, and get the
archive back as a normal browser download under the service's default name.

**Why two requests.** Browsers send a whole request body before reading any of the
response, so one upload request would be limited by the service's output buffer
(contract §4.1). Each job is therefore two requests running at the same time, which
`browser_jobs.py` joins into one streaming exchange with the service:

1. `POST /docs/zip/jobs` `{"service": "claude"}` creates the job and returns its URLs.
2. A hidden frame loads `GET /docs/zip/jobs/<id>/archive`. The browser saves the
   archive as it streams in, at any size, without holding it in memory.
3. `XMLHttpRequest` posts the files to `POST /docs/zip/jobs/<id>/files`, showing upload
   progress. It is answered after the exchange, with JSON: `outcome` (`completed`,
   `rejected` with the service's problem, `aborted`, `cancelled`), the file name and the
   byte counts.

The upload request's body goes through `ServiceProxy.forward`, the same streaming path as
the API. The response passes through a one-message pipe to the download, so backpressure
holds end to end. A download cut mid-way, or cancelled by the user, ends the exchange at
once.

**Security:**
- **Jobs:** unguessable IDs (192 bits), bound to the signed-in user (another user gets
  404), expired if both halves do not arrive within 30 s.
- **Creating and uploading:** need the session cookie, an `X-CSRF-Token` derived from it
  (`SessionSigner.csrf_token`), and a same-origin `Origin`.
- **The page:** the only portal page with script (its own file, `zip.js`, no inline
  code), with a CSP that allows only this origin.

**Limits:** `GATEWAY_BROWSER_JOBS_MAX` jobs per gateway task (default 16), and 4 per user.
Behind the load balancer, sticky sessions keep both halves of a job on the same task.

## Daily quota

Each user may move `GATEWAY_USER_DAILY_QUOTA_MIB` (default 10 GiB) per UTC day: upload
bytes plus archive bytes, across all their keys and the browser page
(`accounts/quota.py`). It caps data transfer out, the one AWS cost that grows with use.

- **Checked when a request starts:** over quota, the API returns `429 quota-exceeded`
  (`urn:zip-gateway:problem:quota-exceeded`) with `Retry-After` until 00:00 UTC, and the
  browser page shows the same message. A request already running is never cut off, so a
  user can go over by what their requests in flight move.
- **Counted when a request ends:** in memory, then written to `usage_daily` about once a
  minute (one upsert per user), like key usage. Each task rereads a user's total at most
  once a minute, so with several gateway tasks the limit is approximate by up to a
  minute's traffic.
- **Who:** users (`user:<sub>`). Operator keys, and keys created by an operator session,
  are exempt: they are automation.
- **Shown:** users see today's total on `/docs/keys`; admins see everyone's on
  `/docs/admin/users`. Rows are kept 90 days.
- **Fails open:** if the database can't be read, the request goes ahead and the error is
  logged. A cost control must not take the API down.

## API keys for users

Signed-in users manage their own keys on `/docs/keys` (create, see usage, revoke). The
API accepts them exactly like operator keys: `Authorization: Bearer <key>`.

- **Format:** `zk_<prefix>_<secret>`. The 8-character prefix is the key's ID: it is shown
  on pages and in logs, and it isn't secret. The secret is 32 random bytes.
- **Storage:** only SHA-256 of the secret is stored, in the accounts store
  (`accounts/store.py`: Postgres, or SQLite in memory in tests). The full key appears once, in the
  response that creates it. A fast hash is right for 256 random bits; a slow password hash
  would only add latency to every request.
- **Checking:** look up the prefix, compare hashes in constant time, and check the key
  isn't revoked. Results are cached for 60 s per gateway task, so a revocation made on
  another task takes up to a minute to apply (immediately on the task that made it).
- **Usage:** requests are counted in memory and written once a minute per key (`ADD
  use_count`, `SET last_used`), not once per request.
- **Limits:** 5 active keys per user. Names are 1-64 plain characters.
- **Operator keys** (`GATEWAY_API_KEYS`, from Secrets Manager) keep working, for
  automation such as the smoke tests. They are checked first, and logged as
  `operator:<name>`. User keys are logged as `key:<prefix> (<owner>)`.

## Accounts and sign-in

With Cognito configured (`GATEWAY_COGNITO_*`), people get accounts only through invite
links, sign in on Cognito's hosted page, and admins see who signed in and which keys are
used. Without it, the portal offers API-key sign-in only.

- **Sign-in** (`accounts/oidc.py`): OpenID Connect, authorization code with PKCE.
  `/docs/auth/start` keeps state, nonce and the PKCE verifier in a signed, 10-minute
  cookie and sends the browser to Cognito. `/docs/auth/callback` exchanges the code (with
  the client secret) and verifies the ID token: RS256 against Cognito's JWKS (cached for
  an hour, refetched on an unknown key ID), issuer, audience, expiry, `token_use=id` and
  the nonce. The gateway then sets its own session cookie (sub, email, admin flag) and
  keeps nothing from Cognito. Signing out also ends the Cognito session.
- **Sessions** are SameSite=Lax (Strict would drop the cookie on the redirect chain that
  starts at Cognito). Every state-changing form and XHR also needs a same-origin
  `Origin` and the session's CSRF token, an HMAC of the session cookie.
- **Invite links** (`accounts/invites.py`): an admin creates a one-time link with a label
  and an expiry of 1-30 days, for someone whose email they don't know. Whoever opens
  `/docs/invite/<token>` enters their email; the gateway marks the invite used in one
  conditional UPDATE (so it works once, even when submitted twice at once), then calls
  `AdminCreateUser`, and Cognito emails a temporary password. If that email already has
  an account, or Cognito fails, the claim is undone and the link still works. Only
  SHA-256 of the token is stored.
- **Users** are recorded at their first sign-in, linked to their invite through the
  `custom:invite_id` attribute. Every sign-in updates the count and last-login time and
  adds a login event (IP address and browser), kept for 90 days.
- **Admin pages** (Cognito group `admin`): `/docs/admin/invites` (create, see who used
  each, revoke), `/docs/admin/users` (sign-in times and history, disable) and
  `/docs/admin/keys` (every key's owner, last use and request count, revoke). Disabling a
  user disables them in Cognito, signs them out everywhere and revokes their keys; their
  portal session ends within a minute (at once on the task that did it).
- **API-key sign-in** stays, for operators and the smoke tests
  (`GATEWAY_ALLOW_KEY_LOGIN=false` turns it off). It never grants admin.
- **The first admin** is created with `make dev-admin EMAIL=...` (the dev pool) or
  `make aws-admin EMAIL=...` (prod), from the platform repository.

**Local runs with real sign-in:** `make dev-auth` in the platform repository creates a
free dev pool (`terraform/dev`, local state) and writes its settings into `.env`,
including an IAM user that can only manage users in that pool. Open the portal at
exactly `http://localhost:8080` (the pool's callback URL): on `127.0.0.1` the sign-in
cookie would not come back from Cognito. `make dev-auth-down` removes it.

## Accounts database

User API keys (and, with Cognito, users, invites and logins) live in Postgres, accessed
through SQLAlchemy Core with asyncpg: **Aurora Serverless v2** on AWS, a `postgres`
container in local compose, and SQLite in memory for the unit tests. The same code and
migrations run on all three.

- **Migrations:** Alembic, in `gateway/accounts/migrations/versions/`, written by hand.
  They run at startup, before the gateway serves. On Postgres an advisory lock makes
  gateway tasks migrate one at a time. Migrations must stay compatible with the previous
  release, which keeps serving during a rolling deploy: add first, remove later.
- **Aurora pausing:** the cluster scales to zero when idle. The first query afterwards
  waits about 15 s while it resumes, so connections wait up to 30 s. Health checks and
  operator keys never touch the database, so neither the load balancer nor the smoke
  tests keep it awake.
- **Tests:** `make test` uses SQLite. `make test-postgres` runs the store tests against a
  throwaway Postgres container.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GATEWAY_UPSTREAMS` | required | `name=http://host:port,...`. `docs`, `health`, `static` and `api` are reserved names. |
| `GATEWAY_API_KEYS` | required | `name:secret,...`. Each secret must be at least 32 characters. |
| `GATEWAY_SESSION_SECRET` | required | At least 32 characters. Signs the docs session cookie. |
| `GATEWAY_SESSION_TTL_SECONDS` | `43200` | Docs session lifetime. |
| `GATEWAY_COOKIE_SECURE` | `true` | Set to `false` only for plain-HTTP local runs. |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | `5` | Upstream connect timeout. A timeout returns `502 upstream-unavailable`. |
| `GATEWAY_IDLE_TIMEOUT_SECONDS` | `60` | No bytes moving in either direction. Must exceed the services' 30 s. |
| `GATEWAY_BROWSER_JOBS_MAX` | `16` | Concurrent browser upload jobs per gateway task (`/docs/zip`). |
| `GATEWAY_DATABASE_URL` | SQLite in memory | Accounts data: `postgresql+asyncpg://...` (SQLite only for tests and throwaway runs). |
| `GATEWAY_DB_HOST`, `_PORT`, `_NAME`, `_USER`, `_PASSWORD` | | On AWS instead of a URL: Aurora's endpoint, with the user and password from Secrets Manager. |
| `GATEWAY_DB_SSL` | `verify-full` | With `GATEWAY_DB_HOST`: TLS to the database. `verify-full` checks the certificate and hostname against the RDS CAs in `gateway/certs/`; `require` encrypts without checking; `disable`. |
| `GATEWAY_COGNITO_DOMAIN`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_USER_POOL_ID` | | All or none. The hosted login URL (`https://<prefix>.auth.<region>.amazoncognito.com`), the app client and the pool. Turns on sign-in with Cognito and invite links. |
| `GATEWAY_PUBLIC_URL` | | Required with Cognito: `https://zip.example.com` (no path). Builds the callback URL and invite links. |
| `GATEWAY_ALLOW_KEY_LOGIN` | `true` | Whether the portal also accepts an operator API key to sign in. |
| `GATEWAY_USER_DAILY_QUOTA_MIB` | `10240` | Bytes each user may move per UTC day (uploads plus archives); `0` for no limit. Operator keys are exempt. |
| `SHUTDOWN_GRACE_SECONDS` | `25` | uvicorn's graceful shutdown on SIGTERM. |
| `PORT`, `LOG_LEVEL` | `8000`, `INFO` | |

Invalid configuration exits with status 2 and a message on stderr. Logs are JSON lines
on stdout, with one `request finished` line per proxied request. That line has `service`,
`api_key` (the key's name), `status`, `bytes_in`, `bytes_out`, `duration_ms`, `outcome`
and `code`.

## Deployment

The gateway deploys to AWS from the platform repository's workflows, independently of
the zip services, onto the shared platform (ECS Fargate behind an HTTPS load balancer).
`deploy/` uses the platform's `ecs-service` module by relative path
(`../../terraform/modules/ecs-service`), so a module change and the gateway change that
needs it land in one pull request.

- **`.github/workflows/gateway.yml`** (repository root): `make -C gateway check` in the
  dev image, then the runtime image must start read-only with no capabilities.
- **`.github/workflows/plan-apply.yml`:**
  - on pull requests touching the gateway: `terraform plan` of `gateway/deploy`
    (read-only role)
  - on merge to `main`: the platform apply first if `terraform/` changed, then the
    gateway checks, then: push the image to ECR tagged with the commit, `terraform apply`
    of `gateway/deploy` (waits for a healthy task; rolls back if there never is one), and
    a check through the load balancer that health, authentication and the portal answer.
    It assumes the gateway's own narrow deploy role, not the platform's.

The gateway runs on on-demand Fargate (the entry point is never reclaimed like Spot), with
one task of 0.25 vCPU and 0.5 GB, as the load balancer's only target. API keys and the
session secret come from Secrets Manager (`zip-platform/prod/gateway`, written by the
platform's `make aws-api-keys`). Upstreams are the zip services' Cloud Map names,
`<service>.zip.internal:8000`. Behind the load balancer, `X-Forwarded-Proto` and the `Host`
header give the docs portal its public URL.

Everything else comes from the platform's SSM parameters (`/zip-platform/prod/*`): the
Cognito pool, app client and hosted-login domain, and Aurora's endpoint and user. The two
secrets, the Cognito client secret (`zip-platform/prod/cognito`) and the database
password (`zip-platform/prod/database`), are injected by ECS from Secrets Manager. The
task role (the module's `task_role_policy_json`) allows only `AdminCreateUser`,
`AdminDisableUser` and `AdminUserGlobalSignOut` on the prod pool. `GATEWAY_PUBLIC_URL` is
`https://<domain>`.

## Known limitations

- **Stalled clients.** If a client stops reading during a mid-stream abort, uvicorn waits
  for the unsent bytes to flush before closing. The load balancer's idle timeout ends
  those connections.
- **Rate limits.** Key sign-in and invite redemption allow 10 posts a minute per client IP
  (`ratelimit.py`), in memory per gateway task. Guessing is not the threat there: API keys
  are at least 32 random characters and invite tokens 192 bits. API calls themselves are
  not rate-limited; the services cap concurrent zips. For abuse at volume, add an AWS WAF
  rate rule in front of the load balancer.
- **Email volume.** Cognito's default sender allows about 50 emails a day, plenty for
  invites. Beyond that, configure SES on the user pool.
- **TLS.** The gateway speaks plain HTTP to the services. Traffic stays inside the VPC,
  with security groups allowing only gateway → service. To Aurora it uses TLS, verified against
  the RDS CA bundle in `gateway/certs/`.
