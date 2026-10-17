# Deploying to AWS

Four repositories: this one (the shared platform and the gateway), and one per zip
service, each deploying on its own. All infrastructure is Terraform; the AWS console is
used only for the first-time steps below.

```text
Route 53 (your domain) ─▶ ALB :443 (ACM certificate, HTTP→HTTPS) ─▶ gateway   on-demand Fargate
                                                                      │  <name>.zip.internal (Cloud Map DNS)
                                                                      ▼
                                                     claude · grok · dsh   Fargate Spot
VPC: 2 AZs. Public subnets: ALB and one NAT gateway. Private subnets: every task.
Security groups: internet → ALB 443/80 → gateway 8000 → services 8000; gateway → Aurora 5432.
Nothing else.
```

| Repository | Owns | Deployed by |
|---|---|---|
| **zip-platform** (this one) | `terraform/bootstrap` (state, CI roles, ECR, budget, kill switch), `terraform/prod` (network, load balancer, DNS, certificate, cluster, Cloud Map, Cognito, Aurora, secrets), `terraform/modules/*`, and the **gateway** (`gateway/`, with its stack in `gateway/deploy`) | an administrator once (`make aws-bootstrap`), then `plan-apply.yml` on merge: the platform first, then the gateway, each only if it changed |
| **zip-claude**, **zip-grok**, **zip-dsh** | their service, and `deploy/`, which calls the platform's `ecs-service` module at a release tag | their own `deploy.yml`, after their CI passes on `main` |

The platform hands its outputs to the services as SSM parameters
(`/zip-platform/prod/...`). A service reads parameters, never another stack's state, so
the repositories are coupled only by those names and the module's inputs.

The gateway lives here because it changes with the platform: accounts needed Cognito,
Aurora, new parameters and a module change together. It uses the module by relative
path, so one pull request carries all of it. The zip services stay separate: each shows
one implementation of the same contract, with its own history and CI.

On disk the zip-service repositories sit side by side inside this one, each in its own
directory (`claude/`, `grok/`, `dsh-qwen3.8/`), which this repository's `.gitignore`
excludes. `docker compose` and `make aws-down` rely on that layout.

## First-time setup

### In the AWS console (the only manual steps)

1. **Secure the account:** turn on MFA for the root user. Create an administrator for
   your own use (IAM Identity Center is best) and configure a CLI profile for it
   (`aws configure sso`).
2. **Register the domain:** in Route 53, go to Registered domains → Register. This costs
   about $14/year for `.com`, and creates the hosted zone. Confirm the registrant email.

### On your machine

All you need is Docker. Every tool runs in a container.

```bash
cp platform.env.example platform.env                          # AWS_REGION, AWS_PROFILE
cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars
cp terraform/prod/terraform.tfvars.example terraform/prod/terraform.tfvars
# edit both tfvars: region, alert email, owner/name of the four repositories, domain

make aws-bootstrap      # state bucket, OIDC + roles, ECR, $100 budget and kill switch
                        # then click the confirmation link AWS emails you (alerts)
make aws-apply          # the shared platform (first time from here; CI afterwards),
                        # including the Cognito pool and the Aurora accounts database
make aws-api-keys NAMES="smoke you"     # prints the keys once: keep them
make aws-admin EMAIL=you@example.com    # your portal admin account; Cognito emails a password
```

### In GitHub

1. Create the four repositories and push each one.
2. In **each** repository, create an environment named `prod`. Add yourself as a required
   reviewer if deploys should wait for approval.
3. Set repository variables:

   | Variable | Where | Value |
   |---|---|---|
   | `AWS_ACCOUNT_ID`, `AWS_REGION` | all four | your account and region |
   | `DOMAIN_NAME` | all four | e.g. `zip.example.com` |
   | `CLAUDE_REPOSITORY`, `GROK_REPOSITORY`, `DSH_REPOSITORY` | platform | `owner/name`, for the integration workflow |
   | `SMOKE_SERVICES` | platform | `claude,grok` (add `dsh` once deployed) |

4. Set the secret `SMOKE_API_KEY` (the `smoke` key) in all four repositories. If the
   repositories are private, also give the platform repository a fine-grained read-only
   token as `REPOS_READ_TOKEN`.
5. In every zip service's `deploy/main.tf`, replace `OWNER` in the module source. Then
   tag the platform repository, `git tag v1.0.0 && git push --tags`, which the services
   pin. Private repositories also need a token for `git::https` module downloads. (The
   gateway uses the module by relative path, so it needs no tag.)

### First deploy

1. Merge to `main` here. `plan-apply` applies the platform, runs the gateway checks, then
   deploys the gateway: push the image, `terraform apply`, and a post-deploy check.
   Then merge to `main` in **claude** and **grok**, which deploy themselves the same way.
2. Open `https://<domain>/docs` and sign in with your admin account. Create invite
   links on Admin → Invites.
3. Run `live-smoke` from the platform repository's Actions tab. It includes a 32 MiB
   full-duplex upload through the load balancer, which proves streaming end to end on
   AWS.

## Day to day

- **Changing a service:** merge to its `main`. Only that service is redeployed. Its
  image is tagged with the commit, and the circuit breaker rolls back a deploy whose
  tasks never become healthy.
- **Changing the platform or the gateway:** open a pull request in this repository.
  `plan-apply` shows a plan for whichever changed (and `gateway` runs its checks). On
  merge it applies the platform, then deploys the gateway.
- **Changing the module:** the gateway picks it up in the same pull request. For the zip
  services, release a new tag of this repository, then bump `?ref=` in each one when it is
  ready. They move independently.
- **API keys:** `make aws-api-keys NAMES="smoke alice"` replaces the key list and
  restarts the gateway.
- **Logs:** CloudWatch log groups `/zip-platform/prod/<service>`, JSON lines. Follow one
  request across the gateway and a service in Logs Insights with
  `filter request_id = "<id>"`.

## Switching it off

| Command | Effect | Cost while off |
|---|---|---|
| `make aws-pause` | every service scaled to 0; `make aws-resume` restores them | ~$2.2/day (ALB, NAT) |
| `make aws-down` | destroys every service stack and `terraform/prod` | ~$1/month (state, ECR images, zone) |
| `make aws-nuke` | `aws-down`, then the bootstrap stack too | the domain only (~$14/year, turn off auto-renew) |

- **Coming back after `aws-down`:** run `make aws-apply`, then `make aws-api-keys`. The
  secret is recreated empty, and the gateway won't start without keys. Then
  `make aws-admin EMAIL=...`: the user pool and the database were destroyed too, so every
  account, invite and user key is gone. Then redeploy by hand, gateway first: `plan-apply`
  with target `gateway` here, then each zip service's `deploy` workflow.
- **After `aws-nuke`:** the script prints the two remaining manual steps. Turn off domain
  auto-renew, and delete the hosted zone with one CLI command.

## Cost controls

See [costs.md](costs.md): what it costs, the $100 budget and kill switch, and why there
is no true hard cap on AWS.

## Adding a service

1. Add it to `github_repositories` in `terraform/bootstrap/terraform.tfvars`, then
   `make aws-bootstrap` (its ECR repository and roles).
2. Add its `deploy/` stack and `deploy.yml` to its repository (copy a zip service's).
3. Add it to the gateway's `GATEWAY_UPSTREAMS` in `gateway/deploy/main.tf`.

## Accounts and sign-in

People sign in to the portal with Cognito, and get an account only through an invite
link from an admin (see the gateway README, "Accounts and sign-in").

- **The pool:** `terraform/prod/auth.tf` (module `terraform/modules/cognito-pool`):
  invite-only, email as username, Cognito's hosted login, group `admin`, and an app
  client for the gateway. Its IDs are published as SSM parameters
  (`/zip-platform/prod/cognito_*`); the client secret goes into Secrets Manager
  (`zip-platform/prod/cognito`), so it is also in this stack's encrypted state.
- **The first admin:** after `make aws-apply`, run `make aws-admin EMAIL=you@example.com`.
  Cognito emails a temporary password; sign in at `https://<domain>/docs/login` and
  choose your own. Then create invite links on `/docs/admin/invites`.
- **Locally:** `make dev-auth` creates a separate, free dev pool (`terraform/dev`, local
  state, callback `http://localhost:8080`) and writes its settings into `.env`. Then
  `make up` and `make dev-admin EMAIL=you@example.com`. `make dev-auth-down` removes it;
  `make aws-nuke` does too.
- **Teardown:** the prod pool goes with `make aws-down`, and with it every account and
  invite. That is intended: nothing outlives the interview.
- **The database:** `terraform/prod/database.tf`. Aurora PostgreSQL Serverless v2, one
  instance in the private subnets, reachable only from the gateway's security group,
  encrypted at rest, verified TLS from the gateway. It scales to 0 ACU and pauses after 10 idle
  minutes; the first request after that waits ~15 s. The gateway runs the migrations at
  startup. The password is generated as an ephemeral value and written only to the
  cluster and to Secrets Manager (`zip-platform/prod/database`), so it is never in
  Terraform state. To change it, bump `database_password_version` in `database.tf`, apply,
  and restart the gateway.
- **The gateway's AWS permissions:** a task role (the module's `task_role_policy_json`)
  that may only create, disable and sign out users in the prod pool.
- **Email:** Cognito's default sender, about 50 emails a day. Enough for invites.
- **Cost:** $0. Under 10,000 monthly active users the Lite tier is free.
