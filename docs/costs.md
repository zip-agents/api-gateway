# Costs and cost controls

## What it costs

Rough monthly figures for one always-on `prod` environment (us-east-1 / eu-west-2,
on-demand prices):

| Item | Monthly | Notes |
|---|---|---|
| NAT gateway, plus its public IP | ~$37 | plus $0.045/GB processed (image pulls, AWS API calls) |
| Application Load Balancer, plus 2 public IPs | ~$29 | hourly charge, minimum capacity-unit charge, IPv4 |
| gateway: 1 task, 0.25 vCPU / 0.5 GB, on-demand | ~$9 | never reclaimed, as the public entry point |
| claude, grok, dsh: 1 task each, 0.25 vCPU / 1 GB, Fargate Spot | ~$9 | ~70% below on-demand |
| CloudWatch Logs (14 days), Secrets Manager, ECR, Route 53 zone | ~$3 | |
| Accounts database: Aurora Serverless v2, paused when idle | ~$1–5 | storage ~$0.11/GB; $0.14 per ACU-hour only while awake (0.5–2 ACU, pauses after 10 idle minutes) |
| Cognito user pool (Lite) | $0 | free under 10,000 monthly active users |
| **Total, idle** | **~$89** | |
| Data transfer out (archives downloaded) | +$0.09/GB | the only cost that grows with use |

A registered `.com` domain adds about $14/year.

## Controls

1. **Nothing can scale up by itself.**
   - Every service runs a fixed number of tasks, with no autoscaling resources at all.
   - The `ecs-service` module rejects more than 2 tasks per service.
   - Task sizes are small and fixed.
   - Terraform ignores the running count after creation, so a deploy never undoes a
     pause or the kill switch.

   Compute therefore has a known ceiling.
2. **Traffic needs an API key.** Every request through the gateway is authenticated, and
   each service limits concurrent zips (`MAX_CONCURRENT_REQUESTS`). Data transfer can
   only come from key holders.
3. **A daily byte quota per user** (`GATEWAY_USER_DAILY_QUOTA_MIB`, default 10 GiB):
   uploads plus archives, across all of a user's keys and the browser page. Over it,
   requests get `429 quota-exceeded` until midnight UTC. At $0.09/GB out, a user
   costs at most about $1 a day in data transfer. Operator keys are exempt, so keep
   them to automation.
4. **A budget with a kill switch** (`terraform/bootstrap/cost.tf`, $100/month):
   - 50% and 80% of actual spend: email
   - 100% of forecast spend: email
   - 100% of actual spend: email, and the **kill switch**. This Lambda scales every
     service to zero, which stops compute and data transfer. The load balancer and NAT
     keep running, at ~$2.2/day, until `make aws-down`. Restore with `make aws-resume`.
   - `make aws-test-killswitch` triggers it by hand, to check it works.

   The budget and kill switch are in the bootstrap stack, so they survive `make aws-down`.
5. **Switching off:** `make aws-pause`, `make aws-down` and `make aws-nuke`
   (docs/deployment.md).

## Not a true hard cap

AWS has no hard spending limit, and billing data lags by several hours (sometimes up to a
day). The kill switch therefore fires at "budget plus however much accrued in the lag".
With compute capped by design, the lag mostly matters for data transfer, and the gateway
caps that per user (control 3 above).

## Trade-offs made for cost

| Choice | Saving | Cost of the choice |
|---|---|---|
| One NAT gateway, not one per AZ | ~$37/month | If its AZ fails, tasks lose outbound access (image pulls, logs, secrets). Running tasks keep serving. |
| Fargate Spot for the zip services | ~$20/month | A task can be reclaimed with 2 minutes' notice. ECS replaces it; in-flight streams to that service are cut. |
| One task per service | ~$40/month | No redundancy within a service. A deploy starts the new task before stopping the old one, so there's no downtime on deploy. |
| Aurora pausing at 0 ACU, one instance | ~$45/month (vs. 0.5 ACU always on) | The first request after 10 idle minutes waits ~15 s while it resumes. No standby: if the instance fails, sign-in and user keys are down until Aurora replaces it (operator keys keep working). |
| Container Insights off | ~$5–10/month | No per-task CPU/memory dashboards. The services' JSON logs carry the request-level data. |
| No WAF | ~$10+/month | No managed rule sets or rate limiting at the edge. If added, exclude `SizeRestrictions_BODY`, which blocks uploads over 8 KB. |
