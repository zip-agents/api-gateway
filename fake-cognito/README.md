# fake-cognito

A local stand-in for Amazon Cognito, so the platform runs with no cloud account:
`make up` starts it with the rest of the stack. **Development only.** It refuses to start
without `FAKE_COGNITO_DEV_ONLY=yes`, and compose publishes it on 127.0.0.1 only.

It implements the parts of Cognito's contract the gateway uses, at the same paths and in
the same formats, so the gateway runs its production code against it:

| Cognito | Here |
|---|---|
| Hosted login: `/oauth2/authorize` (code + PKCE S256), first-sign-in password change | a plain sign-in page with the same steps |
| `/oauth2/token` (client secret, PKCE verifier, single-use codes) | same |
| `/logout` (`client_id`, registered `logout_uri`) | same |
| `/<pool>/.well-known/jwks.json` | same; the RS256 key persists in the volume |
| ID token claims: `iss` (`https://cognito-idp.<region>.amazonaws.com/<pool>`), `aud`, `token_use=id`, `nonce`, `email`, `cognito:groups`, `custom:invite_id` | same |
| Admin API, AWS JSON 1.1: `AdminCreateUser` (incl. `MessageAction=SUPPRESS`), `AdminSetUserPassword`, `AdminDeleteUser`, `AdminGetUser`, `AdminDisableUser`, `AdminEnableUser`, `AdminUserGlobalSignOut`, `AdminAddUserToGroup`, `AdminListGroupsForUser` | same requests, responses and modelled errors (`UsernameExistsException`, ...) |
| Invitation email with a temporary password (admins made by `make dev-admin`; people joining with a code get none) | an outbox: `http://localhost:9229/outbox` (and `/outbox.json`) |

Not implemented: MFA, the forgotten-password flow, other API calls, and checking request
signatures. Users, the signing key and the outbox persist in the `fake-cognito-data` volume
(`docker compose down -v` resets them, along with the gateway's database).

- `make dev-admin EMAIL=you@example.com` creates an admin with the real AWS CLI.
- `make fake-cognito-test` runs the contract tests: boto3 (as the gateway's
  `directory.py`) and httpx with PyJWT (as its `oidc.py`) against the fake.
- `smoke/test_accounts.py` runs the whole account flow through the gateway against it.

What it can't tell you is whether this reading of Cognito is right. The AWS rehearsal
checks that, against the real pool from `terraform/modules/cognito-pool`.
