# Authentication and access

Two separate doors, with separate credentials:

- **The API** (`/<service>/api/v1/zip`) takes only `Authorization: Bearer <key>`. Cookies
  are never accepted there, so a signed-in browser can't be made to call the API from
  another site.
- **The portal** (`/` home, `/zip`, `/account/...` and the docs under `/docs/...`) takes
  only the gateway's own session cookie. Browsers get
  it by signing in with Cognito, or, locally only, with an operator API key (off in prod).

Code: `gateway/gateway/auth.py` (keys, sessions, CSRF), `gateway/gateway/accounts/`
(Cognito, invites, user keys, quota). Infrastructure: `terraform/modules/cognito-pool`.

**Locally there is no cloud:** `fake-cognito/` stands in for Cognito at the same paths
and in the same formats, so every diagram below holds with "Cognito" read as
fake-cognito (`http://localhost:9229`, outbox at `/outbox`). Real Cognito is checked on AWS.

## Credentials at a glance

```mermaid
flowchart LR
    subgraph clients[Clients]
        script["Script or curl"]
        browser["Browser"]
    end

    subgraph gateway[Gateway]
        api["API<br/>/{service}/api/v1/zip"]
        portal["Portal<br/>/  /zip  /account  /docs"]
    end

    subgraph zip[Zip services]
        services["claude · grok · dsh"]
    end

    subgraph stores[Where secrets live]
        sm[("Secrets Manager<br/>operator keys,<br/>session secret")]
        db[("Aurora<br/>SHA-256 of user keys<br/>and invite codes, users,<br/>sign-ins, daily usage")]
        cognito[("Cognito<br/>accounts and passwords,<br/>group admin")]
    end

    script -- "Bearer operator key<br/>or user key zk_…" --> api
    api -- "valid key:<br/>streamed both ways" --> services
    browser -- "session cookie<br/>gw_docs_session" --> portal
    browser -. "signs in at the<br/>hosted login page" .-> cognito

    api -- "operator key: compared<br/>with the keyring" --> sm
    api -- "user key: hash lookup,<br/>quota" --> db
    portal -- "session signed with<br/>the session secret" --> sm
    portal -- "users, invites,<br/>keys, usage" --> db
    portal -- "create and disable users<br/>(task role)" --> cognito
```

| Credential | Who has it | Stored as | Gets you |
|---|---|---|---|
| Operator API key | automation (smoke tests), operators | plain, in Secrets Manager (`GATEWAY_API_KEYS`) | the API, no quota. Locally also portal sign-in without admin; in prod key sign-in is off (`GATEWAY_ALLOW_KEY_LOGIN=false`) |
| User API key `zk_<prefix>_<secret>` | a user, made on `/account/keys` (at most 5) | SHA-256 of the secret, in Aurora | the API, counted against the user's daily quota |
| Cognito password (+ optional TOTP) | a user, chosen when joining | in Cognito only | portal sign-in; admin pages if in group `admin` |
| Session cookie `gw_docs_session` | a signed-in browser | nowhere: HMAC-signed, HttpOnly, SameSite=Lax, path `/` (the API never reads it), 12 h | the portal; refused within a minute once the user is disabled or deleted (here or in Cognito), and at once after they sign out |
| Invite code `XXXX-XXXX-XXXX` | whoever an admin gives it to | SHA-256 of the code, in Aurora | one account, with an email and password they choose at `/account/join`, once, before it expires (1–30 days) |

## Signing in with Cognito

Authorization code with PKCE. The gateway verifies the ID token itself, then issues its
own session and keeps nothing from Cognito.

```mermaid
sequenceDiagram
    autonumber
    actor U as Browser
    participant G as Gateway
    participant C as Cognito (hosted login)
    participant DB as Aurora

    U->>G: GET /zip (no session)
    G-->>U: 303 /account/login?next=/zip
    U->>G: GET /account/auth/start?next=/zip
    Note over G: new state, nonce and PKCE verifier,<br/>in a signed 10-minute cookie (path /account/auth)
    G-->>U: 303 to Cognito /oauth2/authorize<br/>(client_id, state, nonce, S256 challenge)
    U->>C: email + password (+ TOTP)
    C-->>U: 302 /account/auth/callback?code&state
    U->>G: GET /account/auth/callback (with the flow cookie)
    Note over G: state must match the flow cookie
    G->>C: POST /oauth2/token (code, verifier, client secret)
    C-->>G: ID token
    Note over G: verify RS256 against Cognito's JWKS,<br/>issuer, audience, expiry, token_use=id, nonce
    G->>DB: user disabled? record the sign-in<br/>(first one links the invite)
    G-->>U: 303 /zip + session cookie<br/>{user:sub, email, admin from group}
```

Signing out clears the cookie and also ends Cognito's own session (`/logout`), so the
next sign-in asks for the password again.

## Getting an account: invite codes

The admin doesn't know the recipient's email, so the recipient chooses it, along with
their password, on the site. Nothing is emailed.

```mermaid
sequenceDiagram
    autonumber
    actor A as Admin
    actor R as Recipient
    participant G as Gateway
    participant DB as Aurora
    participant C as Cognito

    A->>G: POST /account/admin/invites (label, days, CSRF token)
    G->>DB: store SHA-256(code), expiry
    G-->>A: the code (e.g. K7QM-4XHT-9CWD), shown once
    A-->>R: passes on the code and /account/join (e.g. through a recruiter)
    R->>G: GET /account/join
    G-->>R: code, email, password, password again
    R->>G: POST the form (same Origin, 10 posts/min per IP)
    Note over G: email valid, passwords match,<br/>password meets the pool's policy
    G->>DB: claim: one conditional UPDATE<br/>(unused, unexpired, works once even if sent twice)
    G->>C: AdminCreateUser(email unverified, custom:invite_id), no email sent
    G->>C: AdminSetUserPassword(chosen password, permanent)
    alt created
        G-->>R: "your account is ready", then sign in as usual
    else email taken, password refused, or Cognito failed
        G->>C: AdminDeleteUser (only if half-made)
        G->>DB: release the claim (the code works again)
        G-->>R: explain, keeping the code and email (never the password)
    end
```

## Calling the API

```mermaid
sequenceDiagram
    autonumber
    participant S as Script
    participant G as Gateway
    participant DB as Aurora
    participant Z as Zip service

    S->>G: POST /claude/api/v1/zip<br/>Authorization: Bearer {key}
    alt operator key
        Note over G: constant-time compare against the keyring.<br/>No database, no quota.
    else user key zk_{prefix}_{secret}
        G->>DB: look up the prefix (cached 60 s)
        Note over G: compare SHA-256(secret), not revoked,<br/>owner still valid (user active, operator key configured)
        G->>DB: today's bytes for the owner (cached 60 s)
        opt over the daily quota
            G-->>S: 429 quota-exceeded, Retry-After until 00:00 UTC
        end
    else missing or wrong
        G-->>S: 401 unauthorized
    end
    G->>Z: forward, streaming both ways<br/>(Authorization and cookies removed)
    Z-->>G: archive
    G-->>S: archive
    Note over G: count the request's bytes, written to<br/>usage_daily (and key usage) once a minute
```

## The browser upload page

The page's uploads are two requests joined by the gateway (a download and an upload), and
both need the session cookie. Creating a job and uploading also need a same-origin
`Origin` and the session's CSRF token (an HMAC of the session cookie), so another site
can't start one. A job belongs to its user, and its ID is unguessable. The quota applies
here too.

## Checks that run on every portal request

- **Signature and expiry** of the session cookie. An operator session also needs its key
  to still be in the keyring.
- **Users who are disabled, deleted in Cognito, or signed out since** are refused. The
  check asks the database and Cognito when a request comes in, keeps the answer 60 s per
  user and task (so a busy user costs one lookup a minute, an idle one none), and is
  immediate on the task where the change was made.
- **Admin pages** need membership of the Cognito `admin` group, looked up the same way
  for admin sessions only: removing someone from the group applies within a minute;
  adding someone applies at their next sign-in. If Cognito can't be reached, admin rights
  wait until it answers.
- **Signing in and out** need a same-origin `Origin`, so another site can't sign a visitor
  in or out.
- **Every state-changing form** needs `Origin` equal to the gateway's public origin, plus
  the CSRF token.
- **Rate limits:** 10 posts a minute per IP on key sign-in (where it is on) and on
  joining with a code.
