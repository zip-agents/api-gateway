# Authentication and access

Two separate doors, with separate credentials:

- **The API** (`/<service>/api/v1/zip`) takes only `Authorization: Bearer <key>`. Cookies
  are never accepted there, so a signed-in browser can't be made to call the API from
  another site.
- **The portal** (`/docs/...`) takes only the gateway's own session cookie. Browsers get
  it by signing in with Cognito, or with an operator API key.

Code: `gateway/gateway/auth.py` (keys, sessions, CSRF), `gateway/gateway/accounts/`
(Cognito, invites, user keys, quota). Infrastructure: `terraform/modules/cognito-pool`.

## Credentials at a glance

```mermaid
flowchart LR
    subgraph clients[Clients]
        script["Script or curl"]
        browser["Browser"]
    end

    subgraph gateway[Gateway]
        api["API<br/>/{service}/api/v1/zip"]
        portal["Portal<br/>/docs/..."]
    end

    subgraph zip[Zip services]
        services["claude · grok · dsh"]
    end

    subgraph stores[Where secrets live]
        sm[("Secrets Manager<br/>operator keys,<br/>session secret")]
        db[("Aurora<br/>SHA-256 of user keys<br/>and invite tokens, users,<br/>sign-ins, daily usage")]
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
| Operator API key | automation (smoke tests), operators | plain, in Secrets Manager (`GATEWAY_API_KEYS`) | the API, no quota; portal sign-in without admin |
| User API key `zk_<prefix>_<secret>` | a user, made on `/docs/keys` (at most 5) | SHA-256 of the secret, in Aurora | the API, counted against the user's daily quota |
| Cognito password (+ optional TOTP) | a user, after an invite | in Cognito only | portal sign-in; admin pages if in group `admin` |
| Session cookie `gw_docs_session` | a signed-in browser | nowhere: HMAC-signed, HttpOnly, SameSite=Lax, path `/docs`, 12 h | the portal; disabled users are refused within a minute |
| Invite link `/docs/invite/<token>` | whoever an admin sends it to | SHA-256 of the token, in Aurora | one account, once, before it expires (1–30 days) |

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

    U->>G: GET /docs/zip (no session)
    G-->>U: 303 /docs/login?next=/docs/zip
    U->>G: GET /docs/auth/start?next=/docs/zip
    Note over G: new state, nonce and PKCE verifier,<br/>in a signed 10-minute cookie (path /docs/auth)
    G-->>U: 303 to Cognito /oauth2/authorize<br/>(client_id, state, nonce, S256 challenge)
    U->>C: email + password (+ TOTP)
    C-->>U: 302 /docs/auth/callback?code&state
    U->>G: GET /docs/auth/callback (with the flow cookie)
    Note over G: state must match the flow cookie
    G->>C: POST /oauth2/token (code, verifier, client secret)
    C-->>G: ID token
    Note over G: verify RS256 against Cognito's JWKS,<br/>issuer, audience, expiry, token_use=id, nonce
    G->>DB: user disabled? record the sign-in<br/>(first one links the invite)
    G-->>U: 303 /docs/zip + session cookie<br/>{user:sub, email, admin from group}
```

Signing out clears the cookie and also ends Cognito's own session (`/logout`), so the
next sign-in asks for the password again.

## Getting an account: invite links

```mermaid
sequenceDiagram
    autonumber
    actor A as Admin
    actor R as Recipient
    participant G as Gateway
    participant DB as Aurora
    participant C as Cognito

    A->>G: POST /docs/admin/invites (label, days, CSRF token)
    G->>DB: store SHA-256(token), expiry
    G-->>A: the link, shown once
    A-->>R: sends the link (e.g. through a recruiter)
    R->>G: GET /docs/invite/{token}
    G->>DB: unused and unexpired?
    G-->>R: "enter your email"
    R->>G: POST email (same Origin, 10 posts/min per IP)
    G->>DB: claim: one conditional UPDATE<br/>(works once, even if sent twice at once)
    G->>C: AdminCreateUser(email, custom:invite_id)
    alt created
        C-->>R: email with a temporary password
        G-->>R: "check your email"
    else email already has an account, or Cognito failed
        G->>DB: release the claim (the link works again)
        G-->>R: explain, and keep the form
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
        Note over G: compare SHA-256(secret),<br/>not revoked, owner not disabled
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
- **Disabled users** are refused. The check is cached 60 s per task, and is immediate on
  the task where the admin disabled them.
- **Admin pages** need the `admin` flag, taken from the Cognito group at sign-in.
- **Every state-changing form** needs `Origin` equal to the gateway's public origin, plus
  the CSRF token.
- **Rate limits:** 10 posts a minute per IP on key sign-in and invite redemption.
