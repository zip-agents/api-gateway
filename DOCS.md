# zip-platform

A platform for three independent implementations of the same zip service (**claude**,
**grok**, **dsh**): one authenticating gateway in front of them, a portal for people,
and the AWS infrastructure, CI/CD and local stack to run it all.

- **The API:** `POST /<service>/api/v1/zip` with `Authorization: Bearer <key>` streams
  the uploaded files back as a zip archive, full duplex and at any size.
- **The portal:** `/` (home), `/zip` (zip files in the browser), `/account/...`
  (sign-in, your API keys, admin pages) and `/docs/...` (the API reference and the
  contract). Sign-in is Cognito, and accounts are by invite code only.
- **Locally:** everything runs in containers with no cloud account. `fake-cognito`
  stands in for Cognito, and Postgres for Aurora.

For a one-page list of commands, see [CHEATSHEET.md](CHEATSHEET.md).

## Contents of this repository

| Path | What it is |
|---|---|
| `gateway/` | The gateway: a streaming reverse proxy (h11, raw ASGI), the portal, accounts, browser upload jobs. Its stack is in `gateway/deploy`. Details are in [gateway/README.md](gateway/README.md). |
| `fake-cognito/` | A local stand-in for the parts of Cognito the gateway uses (hosted sign-in, tokens, keys, admin API), with its own contract tests |
| `terraform/bootstrap` | One-time setup: the state bucket, GitHub OIDC roles, ECR, the $100 budget and the kill switch |
| `terraform/prod` | The shared platform: VPC, ALB, DNS and certificate, ECS cluster, Cloud Map, Cognito, Aurora, secrets |
| `terraform/modules` | `ecs-service` (used by the gateway and each zip service) and `cognito-pool` |
| `lambda/killswitch` | Scales everything to 0 when the budget is exceeded |
| `smoke/` | Smoke tests run through the gateway, locally and against AWS |
| `perf/` | A throughput, first-byte and memory benchmark for each service |
| `contract/`, `gateway/docs/contract.md` | The service contract the three implementations follow |
| `scripts/` | What the `make` targets call (Terraform, AWS, keys, scaling, teardown) |
| `docs/` | [deployment.md](docs/deployment.md), [auth.md](docs/auth.md), [costs.md](docs/costs.md) |
| `.github/workflows/` | CI: checks, gateway image, plan/apply, nightly live smoke, and a manual whole-stack run (`integration`) |
| `claude/`, `grok/`, `dsh-qwen3.8/` | The zip services. These are separate repositories checked out alongside (gitignored here), each with its own CI and `deploy/` |

## Requirements

- Docker, or Podman with `ENGINE=podman`. Every tool (Python, Terraform, the AWS CLI,
  linters) runs in a container, so nothing else is needed on the host.
- For AWS, an AWS account with an administrator CLI profile, and a domain in Route 53.
- `act`, but only if you want to run the GitHub workflows locally (`make act`).

## Running it locally

```bash
make env                                  # writes .env with generated secrets, prints your API key
make up                                   # gateway + claude + grok + dsh + fake-cognito + Postgres
make dev-admin EMAIL=you@example.com      # your local admin account
```

- **The portal:** open `http://localhost:<GATEWAY_PORT>` (8080 by default; set
  `GATEWAY_PORT` in `.env`). Use `localhost` exactly, not `127.0.0.1`: it's the
  registered sign-in callback.
- **First admin sign-in:**
  1. Read the temporary password for the account `make dev-admin` created in
     fake-cognito's outbox, `http://localhost:9229/outbox`.
  2. Sign in and choose your own password.
  3. Create invite codes on Admin → Invites.
- **Key sign-in:** locally you can also sign in with the API key from `.env`. That gives
  a portal session without admin rights.
- **Services under test:** `SMOKE_SERVICES` in `.env` (default `claude,grok,dsh`) picks
  the services `make smoke` and `make perf` cover.
- **Stopping:** `make down` stops the stack, and `make logs` follows its logs.

### Calling the API

```bash
KEY=<the key make env printed>
curl -sS -H "Authorization: Bearer $KEY" \
  -F filename="my files" -F files=@a.txt -F files=@photo.jpg \
  -o out.zip http://localhost:8080/claude/api/v1/zip
```

The `filename` form field names the archive. Duplicate file names are renamed
(`a (1).txt`). Errors are RFC 9457 problem details: `urn:zip-gateway:problem:*` from the
gateway, the service's own types otherwise. Users can create their own keys
(`zk_<prefix>_<secret>`, at most 5, with a daily quota) on `/account/keys`.

## Testing

| Command | What it runs |
|---|---|
| `make test-unit` | Every unit-level check, in containers |
| `make gateway-check` | Gateway: ruff, format, mypy `--strict`, pytest with coverage (what CI runs) |
| `make -C gateway test-postgres` | The gateway's store, account and security tests against a real Postgres |
| `make fake-cognito-test` | fake-cognito's contract tests (boto3, httpx, PyJWT) |
| `make test-lambda` | The kill switch's unit tests |
| `make smoke` | Smoke tests for each service through the gateway (stack up) |
| `make test-smoke` | Start the stack, then smoke-test it |
| `make test` | Everything: the unit checks, then the integrated stack |
| `make tf-check` | Terraform fmt, validate, tflint and trivy. No AWS credentials needed |
| `make actions-lint`, `make shellcheck` | Workflow and shell linting |
| `make ci` | The platform repository's CI checks |
| `make perf` | A benchmark for each service (stack up) |

**Where each kind of test runs.** Each repository's CI tests only its own code, against
the contract: the gateway with fake services, and each zip service against
`contract/`. Each deployment then checks itself through the real gateway: a service's
`deploy.yml`, and `plan-apply.yml` for the gateway. A failure stops that deployment.
`live-smoke.yml` smoke-tests the whole deployed system every night. Testing across
repositories before deployment happens only locally (`make up && make smoke`) or on
demand (the manual `integration` workflow, which takes a commit for each service). It
doesn't run in CI, because it would combine this commit with other repositories'
commits, and its failures could belong to any of them.

`make up` waits for the gateway, Postgres and fake-cognito. A zip service that fails to
build or start is reported as a warning, and the gateway answers 502 for it. The rest
of the stack keeps working.

## Deploying to AWS

The full walkthrough is [docs/deployment.md](docs/deployment.md). In short:

```bash
cp platform.env.example platform.env      # AWS_REGION, AWS_PROFILE
cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars
cp terraform/prod/terraform.tfvars.example terraform/prod/terraform.tfvars

make aws-bootstrap                        # once, as an administrator
make aws-apply                            # the shared platform (CI does this on merge afterwards)
make aws-api-keys NAMES="smoke you"       # operator API keys; printed once
make aws-admin EMAIL=you@example.com      # your portal admin; Cognito emails a temporary password
```

1. Set up GitHub: the environment, variables and secrets listed in deployment.md.
2. Merge to `main`. `plan-apply` applies the platform, then deploys the gateway.
3. Merge to `main` in each zip-service repository, which deploys itself.
4. Sign in at `https://<domain>/account/login` with the emailed password, choose your
   own, and create invite codes on Admin → Invites.

In prod, operator keys (the smoke key among them) reach the API only. Portal sign-in is
Cognito only (`GATEWAY_ALLOW_KEY_LOGIN=false`).

### Running it day to day

| Command | Effect |
|---|---|
| `make aws-status` | Desired and running task counts |
| `make aws-pause` / `make aws-resume` | Scale every service to 0 and back (~$2.2/day fixed while paused) |
| `make aws-api-keys NAMES="..."` | Replace the operator key list and restart the gateway |
| `make aws-plan` | Plan the shared platform |
| `make aws-test-killswitch` | Fire the budget kill switch now (undo with `aws-resume`) |
| `make aws-down` | Destroy the services and the platform; bootstrap stays (~$1/month) |
| `make aws-nuke` | Destroy everything, bootstrap included |

After `aws-down`, run `aws-apply`, `aws-api-keys` and `aws-admin` again. The pool and the
database were destroyed, so every account, invite and user key is gone.

## Costs

About $89/month when idle, mostly the NAT gateway, the load balancer and the always-on
tasks. Aurora pauses about 15 minutes after the last query. A $100 budget with a kill
switch scales everything to 0 if it's exceeded. Details are in [docs/costs.md](docs/costs.md).

## Further reading

- [gateway/README.md](gateway/README.md): the gateway's design, configuration, quotas,
  browser jobs, the accounts database and known limitations.
- [docs/auth.md](docs/auth.md): credentials, sign-in, invite codes and the checks on
  each request, with diagrams.
- [docs/deployment.md](docs/deployment.md): the repositories, first-time setup, day to
  day, switching it off.
- [docs/costs.md](docs/costs.md): what it costs and the trade-offs made for cost.
- [fake-cognito/README.md](fake-cognito/README.md): what the local Cognito covers.
