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
| **zip-agents/api-gateway** (this one) | `terraform/bootstrap` (state, CI roles, ECR, budget, kill switch), `terraform/prod` (network, load balancer, DNS, certificate, cluster, Cloud Map, Cognito, Aurora, secrets), `terraform/modules/*`, and the **gateway** (`gateway/`, with its stack in `gateway/deploy`) | an administrator once, by hand ([aws-runbook.md](aws-runbook.md)), then `plan-apply.yml` on merge: the platform first, then the gateway, each only if it changed |
| **zip-agents/claude-code**, **grok-build**, **dsh-qwen-flash-next** | their service, and `deploy/`, which calls the platform's `ecs-service` module at a release tag | their own `deploy.yml`, after their CI passes on `main` |

The platform hands its outputs to the services as SSM parameters
(`/zip-platform/prod/...`). A service reads parameters, never another stack's state, so
the repositories are coupled only by those names and the module's inputs.

The gateway lives here because it changes with the platform: accounts needed Cognito,
Aurora, new parameters and a module change together. It uses the module by relative
path, so one pull request carries all of it. The zip services stay separate: each shows
one implementation of the same contract, with its own history and CI.

On disk the zip-service repositories sit side by side inside this one, each in its own
directory (`claude/`, `grok/`, `dsh-qwen3.8/`), which this repository's `.gitignore`
excludes. `docker compose` relies on that layout.

## First-time setup

### In the AWS console (the only manual steps)

1. **Secure the account:** turn on MFA for the root user. Create an administrator for
   your own use (IAM Identity Center is best) and configure a CLI profile for it
   (`aws configure sso`).
2. **Register the domain:** in Route 53, go to Registered domains → Register. This costs
   about $14/year for `.com`, and creates the hosted zone. Confirm the registrant email.

### By hand, once

Nothing in the Makefile talks to AWS. Follow [aws-runbook.md](aws-runbook.md) steps 1–5:
tools and credentials, the bootstrap stack (then confirm the alert email AWS sends), the
platform, the operator API keys (the gateway won't start without them), and your portal
admin.

### In GitHub

1. Create the four repositories and push each one.
2. In **each** repository, create an environment named `prod`. Under **Deployment
   branches and tags**, allow only `main`: the deploy roles trust any job that runs in
   `prod`, so without this rule a workflow started on another branch could deploy (for
   the platform, with PowerUser rights). Add yourself as a required reviewer if deploys
   should wait for approval.
3. Set repository variables:

   | Variable | Where | Value |
   |---|---|---|
   | `AWS_ACCOUNT_ID`, `AWS_REGION` | all four | your account and region |
   | `DOMAIN_NAME` | all four | e.g. `zip.example.com` |
   | `CLAUDE_REPOSITORY`, `GROK_REPOSITORY`, `DSH_REPOSITORY` | platform | `owner/name`, for the manual `integration` workflow (the whole stack on a runner) |
   | `SMOKE_SERVICES` | platform | `claude,grok,dsh`: the services the live smoke test covers |

   | `NUKE_AFTER` | platform | the date the `nuke` workflow destroys everything, `YYYY-MM-DD` |

4. Set the secret `SMOKE_API_KEY` (the `smoke` key) in all four repositories. If the
   repositories are private, also give the platform repository a fine-grained read-only
   token as `REPOS_READ_TOKEN`.
5. In every zip service's `deploy/main.tf`, the module source must name
   `github.com/zip-agents/api-gateway.git`. Then tag this repository,
   `git tag v1.0.0 && git push --tags`, which the services pin. The repositories are
   public, so `git::https` module downloads need no token. (The gateway uses the module
   by relative path, so it needs no tag.)

### First deploy

1. Merge to `main` here. `plan-apply` applies the platform, runs the gateway checks, then
   deploys the gateway: push the image, `terraform apply`, and a post-deploy check.
   Then merge to `main` in **claude**, **grok** and **dsh**, which deploy themselves the
   same way. Do this only after the gateway is up: their post-deploy check goes through
   it.
2. Open `https://<domain>` and sign in with your admin account. Create invite
   codes on Admin → Invites. Zip a large file once on `/zip`: `live-smoke` can't cover
   the browser upload on AWS, since operator-key sign-in is off there.
3. Run `live-smoke` from the platform repository's Actions tab. It includes a 32 MiB
   full-duplex upload through the load balancer, which proves streaming end to end on
   AWS.
4. Check that Aurora really pauses: about 15 minutes after the last sign-in, the
   cluster's CloudWatch metric `ServerlessDatabaseCapacity` drops to 0, and its RDS
   events show the pause and the resume (`RDS-EVENT-0370` to `RDS-EVENT-0374`) when you
   next sign in. If capacity never reaches 0, something holds a connection open.

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
- **API keys:** add or remove one operator key by hand ([aws-runbook.md](aws-runbook.md),
  step 4). The others, and every portal session, are unaffected.
- **Logs:** CloudWatch log groups `/zip-platform/prod/<service>`, JSON lines. Follow one
  request across the gateway and a service in Logs Insights with
  `filter request_id = "<id>"`.

## Switching it off

The `nuke` workflow destroys everything that costs money ([scripts/nuke.sh](../scripts/nuke.sh)):
the zip services, the gateway, the whole of `terraform/prod` and every image in ECR. It
checks once a day and runs when the date in `NUKE_AFTER` has passed, or when the month's
actual spend has reached the budget (the kill switch has already scaled everything to 0
by then). It can be run by hand from the Actions tab too. Afterwards it disables
`plan-apply`, `live-smoke` and itself, so nothing brings the platform back; service
deploys fail harmlessly, since the parameters they read are gone.

What it leaves costs nothing, or almost nothing: the bootstrap stack (state bucket, empty
ECR repositories, roles, budget, kill switch), the Route 53 zone ($0.50/month) and the
domain. [aws-runbook.md](aws-runbook.md), step 7, removes those by hand.

To pause instead (everything to 0 tasks, ~$2.2/day for the load balancer and NAT), see
the runbook, step 6.

## Cost controls

See [costs.md](costs.md): what it costs, the $100 budget and kill switch, and why there
is no true hard cap on AWS.

## Adding a service

1. Add it to `github_repositories` in `terraform/bootstrap/terraform.tfvars`, then
   apply the bootstrap stack by hand (its ECR repository and roles).
2. Add its `deploy/` stack and `deploy.yml` to its repository (copy a zip service's).
3. Add it to the gateway's `GATEWAY_UPSTREAMS` in `gateway/deploy/main.tf`.

## Accounts and sign-in

People sign in to the portal with Cognito, and get an account only through an invite
code from an admin (see the gateway README, "Accounts and sign-in").

- **The pool:** `terraform/prod/auth.tf` (module `terraform/modules/cognito-pool`):
  invite-only, email as username, Cognito's hosted login, group `admin`, and an app
  client for the gateway. Its IDs are published as SSM parameters
  (`/zip-platform/prod/cognito_*`); the client secret goes into Secrets Manager
  (`zip-platform/prod/cognito`), so it is also in this stack's encrypted state.
- **The first admin:** after the platform is applied, create it by hand
  ([aws-runbook.md](aws-runbook.md), step 5).
  Cognito emails a temporary password; sign in at `https://<domain>/account/login` and
  choose your own. Then create invite codes on `/account/admin/invites`.
- **Locally:** no AWS at all. `make up` includes **fake-cognito**, a local stand-in that
  speaks the parts of Cognito's API the gateway uses (hosted sign-in, tokens and keys,
  the admin calls), so the gateway runs its production code against it.
  `make dev-admin EMAIL=you@example.com` creates an admin with the real AWS CLI; the
  emails Cognito would send are at `http://localhost:9229/outbox`. Real Cognito is first
  exercised on AWS, by the rehearsal.
- **Teardown:** the prod pool goes with the nuke, and with it every account and
  invite. That is intended: nothing outlives the interview.
- **The database:** `terraform/prod/database.tf`. Aurora PostgreSQL Serverless v2, one
  instance in the private subnets, reachable only from the gateway's security group,
  encrypted at rest, verified TLS from the gateway. It scales to 0 ACU and pauses about
  15 minutes after the last query: an open connection, even an idle one, keeps it awake,
  so the gateway closes its pooled connections after 5 minutes without a query
  (`IdleCloser` in `gateway/accounts/store.py`), and Aurora pauses 10 minutes after that.
  The first request afterwards waits while it resumes: about 15 s, 30 s or more after a
  day paused. The gateway waits up to 60 s for a connection and retries once
  (`gateway/accounts/connect.py`). AWS maintenance wakes it, and it then stays up at
  least 20 minutes. The gateway runs the migrations at
  startup. The password is generated as an ephemeral value and written only to the
  cluster and to Secrets Manager (`zip-platform/prod/database`), so it is never in
  Terraform state. To change it, bump `database_password_version` in `database.tf`, apply,
  and restart the gateway.
- **The gateway's AWS permissions:** a task role (the module's `task_role_policy_json`)
  that may only, in the prod pool: create users with the password they chose (and delete
  one whose password was refused), disable and sign them out, and read whether a user
  still exists, is enabled and is an admin.
- **Email:** Cognito's default sender, about 50 emails a day. Enough for invites.
- **Cost:** $0. Under 10,000 monthly active users the Lite tier is free.
