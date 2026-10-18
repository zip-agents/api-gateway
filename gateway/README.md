# zip-gateway

An authenticating, streaming reverse proxy in front of the three zip service
implementations, plus an authenticated portal with its docs. `/<service>/<path>` is forwarded to
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
make up             # gateway + claude + grok + dsh on http://localhost:8080
make smoke          # smoke tests for each service, through the gateway
make test           # all of the above checks, then the stack and smoke tests
```

Then open <http://localhost:8080> (or your `GATEWAY_PORT`) and sign in: with the key from
`.env`, or as an admin made with `make dev-admin` (see "Accounts and sign-in"). The portal
is laid out as `/` (home), `/zip`, `/account/...` (sign-in, keys, admin) and `/docs/...`
(documentation only).

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

`/zip` (signed in) lets a user pick a service, drop or choose files, and get the
archive back as a normal browser download under the service's default name.

**Why two requests.** Browsers send a whole request body before reading any of the
response, so one upload request would be limited by the service's output buffer
(contract §4.1). Each job is therefore two requests running at the same time, which
`browser_jobs.py` joins into one streaming exchange with the service:

1. `POST /zip/jobs` `{"service": "claude"}` creates the job and returns its URLs.
2. A hidden frame loads `GET /zip/jobs/<id>/archive`. The browser saves the
   archive as it streams in, at any size, without holding it in memory.
3. `XMLHttpRequest` posts the files to `POST /zip/jobs/<id>/files`, showing upload
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
- **The download:** it is a navigation on the portal origin, so the service's response
  is never trusted there. A 200 is forced to an attachment (the service's file name
  kept), every response is sent with `Content-Security-Policy: sandbox`, `nosniff` and
  `no-store`, and a service's `Set-Cookie` is dropped (on the API path too).

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
- **Shown:** users see today's total on `/account/keys`; admins see everyone's on
  `/account/admin/users`. Rows are kept 90 days.
- **Fails open:** if the database can't be read, the request goes ahead and the error is
  logged. A cost control must not take the API down.

## API keys for users

Signed-in users manage their own keys on `/account/keys` (create, see usage, revoke). The
API accepts them exactly like operator keys: `Authorization: Bearer <key>`.

- **Format:** `zk_<prefix>_<secret>`. The 8-character prefix is the key's ID: it is shown
  on pages and in logs, and it isn't secret. The secret is 32 random bytes.
- **Storage:** only SHA-256 of the secret is stored, in the accounts store
  (`accounts/store.py`: Postgres, or SQLite in memory in tests). The full key appears once, in the
  response that creates it. A fast hash is right for 256 random bits; a slow password hash
  would only add latency to every request.
- **Checking:** look up the prefix, compare hashes in constant time, and check the key
  isn't revoked and its owner is still valid: a user who is active (and, with Cognito,
  still exists and is enabled there), or an operator key that is still configured. A key
  made in an operator session stops working when that operator key is removed. Results
  are cached for 60 s per gateway task, so a change made on another task takes up to a
  minute to apply (immediately on the task that made it).
- **Usage:** requests are counted in memory and written once a minute per key (`ADD
  use_count`, `SET last_used`), not once per request.
- **Limits:** 5 active keys per user. Names are 1-64 plain characters.
- **Operator keys** (`GATEWAY_API_KEYS`, from Secrets Manager) keep working, for
  automation such as the smoke tests. They are checked first, and logged as
  `operator:<name>`. User keys are logged as `key:<prefix> (<owner>)`.

## Accounts and sign-in

With Cognito configured (`GATEWAY_COGNITO_*`), people get accounts only through invite
codes, sign in on Cognito's hosted page, and admins see who signed in and which keys are
used. Without it, the portal offers API-key sign-in only.

- **Sign-in** (`accounts/oidc.py`): OpenID Connect, authorization code with PKCE.
  `/account/auth/start` keeps state, nonce and the PKCE verifier in a signed, 10-minute
  cookie and sends the browser to Cognito. `/account/auth/callback` exchanges the code (with
  the client secret) and verifies the ID token: RS256 against Cognito's JWKS (cached for
  an hour, refetched on an unknown key ID), issuer, audience, expiry, `token_use=id` and
  the nonce. The gateway then sets its own session cookie (sub, email, admin flag) and
  keeps nothing from Cognito. Signing out also ends the Cognito session.
- **Sessions** are SameSite=Lax (Strict would drop the cookie on the redirect chain that
  starts at Cognito). Every state-changing form and XHR also needs a same-origin
  `Origin` and the session's CSRF token, an HMAC of the session cookie; signing in and
  out need the same-origin `Origin` too.
- **Looked up on each request, at most once a minute per user** (`accounts/users.py`,
  cached per user and task): a session or user API key works only while the user is
  active in the gateway and, asked of Cognito, still exists, is enabled, and (for admin
  sessions only) is in the `admin` group. Deleting or disabling someone in the Cognito
  console, or removing them from `admin`, applies within a minute rather than when their
  12-hour session expires; someone added to `admin` gets it at their next sign-in. If
  Cognito can't be reached, the gateway's record decides access and admin rights wait
  until Cognito answers. Nothing runs in the background, so the cost follows active
  users, not requests: an idle user costs nothing, a busy one one or two Cognito calls a
  minute, and requests arriving together share a lookup.
- **Signing out ends every session of that user**, not just the browser's copy: sessions
  issued before the sign-out are refused (`sessions_valid_after`), so a copied session
  cookie dies with it. Operator sessions are stateless and end when their key is removed.
- **Invite codes** (`accounts/invites.py`): an admin creates a one-time code, such as
  `K7QM-4XHT-9CWD`, with a label and an expiry of 1-30 days, for someone whose email they
  don't know. Whoever has it opens `/account/join`, enters the code, and chooses their own
  email and password. The gateway marks the code used in one conditional UPDATE (so it
  works once, even when submitted twice at once), then creates the Cognito user with no
  email (`AdminCreateUser` with `MessageAction=SUPPRESS`) and sets the chosen password as
  permanent (`AdminSetUserPassword`). If the email already has an account, the password is
  refused, or Cognito fails, the claim is undone (and a half-made user deleted), and the
  code works again. Only SHA-256 of the code is stored. Nothing is ever emailed, so the
  address is unverified: Cognito's forgotten-password email does not apply, and a user
  who loses their password gets a new code from an admin.
- **Users** are recorded at their first sign-in, linked to their invite through the
  `custom:invite_id` attribute. Every sign-in updates the count and last-login time and
  adds a login event (IP address and browser), kept for 90 days.
- **Admin pages** (Cognito group `admin`): `/account/admin/invites` (create, see who used
  each, revoke), `/account/admin/users` (sign-in times and history, disable) and
  `/account/admin/keys` (every key's owner, last use and request count, revoke). Disabling a
  user disables them in Cognito, signs them out everywhere and revokes their keys; their
  portal session ends within a minute (at once on the task that did it). Admin rights come
  from the Cognito group, looked up again at most a minute apart (see above).
- **API-key sign-in** stays locally and in integration, for the smoke tests. It never
  grants admin. In prod it is off (`GATEWAY_ALLOW_KEY_LOGIN=false` in `deploy/main.tf`):
  operators sign in with their Cognito admin account, and operator keys, the smoke key
  among them, reach the API only.
- **The first admin** is created with `make dev-admin EMAIL=...` (locally) or
  `make aws-admin EMAIL=...` (prod), from the platform repository.

**Locally, with no cloud:** `make up` runs **fake-cognito** (`fake-cognito/` in the platform
repository) in place of Cognito. It implements the parts of Cognito's contract the
gateway uses, at the same paths and in the same formats: the hosted sign-in page (with
the first-sign-in password change), the token endpoint with PKCE, the signing keys, logout,
and the admin API in AWS's JSON protocol. Its ID tokens carry Cognito's exact claims and
issuer. So the gateway runs its production code, with two local settings:
`GATEWAY_COGNITO_DOMAIN=http://localhost:9229` (where the browser goes) and
`GATEWAY_COGNITO_BACKCHANNEL_URL=http://fake-cognito:9229` (where the gateway's token
exchange, key fetch and admin calls go). Emails land in an outbox
(`http://localhost:9229/outbox`). Open the portal at exactly
`http://localhost:<GATEWAY_PORT>`, the registered callback; on `127.0.0.1` the sign-in
cookie would not come back. The fake is checked by its own contract tests
(`make fake-cognito-test`, with boto3, httpx and PyJWT) and by the smoke test of the whole
account flow (`smoke/test_accounts.py`). What it can't tell you is whether our reading of
Cognito is right: the AWS rehearsal checks that.

## Accounts database

User API keys (and, with Cognito, users, invites and logins) live in Postgres, accessed
through SQLAlchemy Core with asyncpg: **Aurora Serverless v2** on AWS, a `postgres`
container in local compose, and SQLite in memory for the unit tests. The same code and
migrations run on all three.

- **Migrations:** Alembic, in `gateway/accounts/migrations/versions/`, written by hand.
  They run at startup, before the gateway serves. On Postgres an advisory lock makes
  gateway tasks migrate one at a time. Migrations must stay compatible with the previous
  release, which keeps serving during a rolling deploy: add first, remove later.
- **Aurora pausing:** the cluster pauses about 15 minutes after the last query (5
  minutes until `IdleCloser` closes the pool, then Aurora's 10). The first query
  afterwards waits while it resumes: about 15 s, 30 s or more after a day paused. So
  connections wait up to 60 s, and opening one is retried once (`accounts/connect.py`);
  a statement is never retried. AWS maintenance wakes it for at least 20 minutes. Health checks and
  operator keys never touch the database, so neither the load balancer nor the smoke
  tests keep it awake.
- **Tests:** `make test` uses SQLite. `make test-postgres` runs the store tests against a
  throwaway Postgres container.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GATEWAY_UPSTREAMS` | required | `name=http://host:port,...`. `docs`, `health`, `static`, `api`, `account` and `zip` are reserved names. |
| `GATEWAY_API_KEYS` | required | `name:secret,...`. Each secret must be at least 32 characters. |
| `GATEWAY_SESSION_SECRET` | required | At least 32 characters. Signs the docs session cookie. |
| `GATEWAY_SESSION_TTL_SECONDS` | `43200` | Docs session lifetime. |
| `GATEWAY_COOKIE_SECURE` | `true` | Set to `false` only for plain-HTTP local runs. |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | `5` | Upstream connect timeout. A timeout returns `502 upstream-unavailable`. |
| `GATEWAY_IDLE_TIMEOUT_SECONDS` | `60` | No bytes moving in either direction. Must exceed the services' 30 s. |
| `GATEWAY_BROWSER_JOBS_MAX` | `16` | Concurrent browser upload jobs per gateway task (`/zip`). |
| `GATEWAY_DATABASE_URL` | SQLite in memory | Accounts data: `postgresql+asyncpg://...` (SQLite only for tests and throwaway runs). |
| `GATEWAY_DB_HOST`, `_PORT`, `_NAME`, `_USER`, `_PASSWORD` | | On AWS instead of a URL: Aurora's endpoint, with the user and password from Secrets Manager. |
| `GATEWAY_DB_SSL` | `verify-full` | With `GATEWAY_DB_HOST`: TLS to the database. `verify-full` checks the certificate and hostname against the RDS CAs in `gateway/certs/`; `require` encrypts without checking; `disable`. |
| `GATEWAY_COGNITO_DOMAIN`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_USER_POOL_ID` | | All or none. The hosted login URL (`https://<prefix>.auth.<region>.amazoncognito.com`, or `http://localhost:<port>` for fake-cognito), the app client and the pool. Turns on sign-in with Cognito and invite codes. |
| `GATEWAY_COGNITO_BACKCHANNEL_URL` | | Locally only: where the gateway itself calls Cognito (token endpoint, keys, admin API) when that differs from the browser's address: `http://fake-cognito:9229`. |
| `GATEWAY_PUBLIC_URL` | | Required with Cognito: `https://zip.example.com` (no path). Builds the callback URL, and the join page address shown with new invite codes. |
| `GATEWAY_ALLOW_KEY_LOGIN` | `true` | Whether the portal also accepts an operator API key to sign in. `false` in prod (`deploy/main.tf`). |
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
header give the portal its public URL.

Everything else comes from the platform's SSM parameters (`/zip-platform/prod/*`): the
Cognito pool, app client and hosted-login domain, and Aurora's endpoint and user. The two
secrets, the Cognito client secret (`zip-platform/prod/cognito`) and the database
password (`zip-platform/prod/database`), are injected by ECS from Secrets Manager. The
task role (the module's `task_role_policy_json`) allows only, on the prod pool:
`AdminCreateUser`, `AdminSetUserPassword` and `AdminDeleteUser` (joining with a code),
`AdminDisableUser` and `AdminUserGlobalSignOut` (disabling), and `AdminGetUser` and
`AdminListGroupsForUser` (the per-request check). `GATEWAY_PUBLIC_URL` is `https://<domain>`.
The load balancer adds `Strict-Transport-Security` to every HTTPS response.

## Known limitations

- **Stalled clients.** If a client stops reading during a mid-stream abort, uvicorn waits
  for the unsent bytes to flush before closing. The load balancer's idle timeout ends
  those connections.
- **Rate limits.** Key sign-in and joining with an invite code allow 10 posts a minute per
  client IP (`ratelimit.py`), in memory per gateway task. API keys are at least 32 random
  characters; invite codes are about 59 bits, expire and work once, so with this limit
  guessing one is hopeless. API calls themselves are
  not rate-limited; the services cap concurrent zips. For abuse at volume, add an AWS WAF
  rate rule in front of the load balancer.
- **Email volume.** Cognito's default sender allows about 50 emails a day, plenty for
  invites. Beyond that, configure SES on the user pool.
- **TLS.** The gateway speaks plain HTTP to the services. Traffic stays inside the VPC,
  with security groups allowing only gateway → service. To Aurora it uses TLS, verified against
  the RDS CA bundle in `gateway/certs/`.
