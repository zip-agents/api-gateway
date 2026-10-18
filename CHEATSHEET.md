# Cheatsheet

Every command runs in containers. Add `ENGINE=podman` to use Podman. `make help` lists
every target, and `make -C gateway help` lists the gateway's. More detail is in
[DOCS.md](DOCS.md).

## Local stack

```bash
make env                                # .env with generated secrets (prints your API key)
make up                                 # start: gateway, claude, grok, dsh, fake-cognito, Postgres
make down                               # stop
make logs                               # follow logs
make dev-admin EMAIL=you@example.com    # local admin; password at http://localhost:9229/outbox
```

| URL | What |
|---|---|
| `http://localhost:<GATEWAY_PORT>` | The portal (use `localhost`, not `127.0.0.1`) |
| `http://localhost:<GATEWAY_PORT>/zip` | Zip files in the browser |
| `http://localhost:<GATEWAY_PORT>/account/keys` | Your API keys |
| `http://localhost:<GATEWAY_PORT>/account/admin/invites` | Invite codes (admin) |
| `http://localhost:<GATEWAY_PORT>/docs/api` | The API reference |
| `http://localhost:9229/outbox` | Emails fake-cognito "sent" |

```bash
curl -sS -H "Authorization: Bearer $KEY" -F filename=out -F files=@a.txt \
  -o out.zip http://localhost:8080/claude/api/v1/zip
```

## Tests and checks

```bash
make test-unit                          # all unit-level checks
make gateway-check                      # gateway lint + format + mypy + tests (CI)
make gateway-test                       # gateway tests only
make -C gateway test PYTEST_ARGS="-k proxy -x"
make -C gateway test-postgres           # store/account/security tests on Postgres
make gateway-fmt                        # apply ruff formatting
make fake-cognito-test                  # fake-cognito contract tests
make test-lambda                        # kill switch tests
make smoke                              # smoke tests (stack up)
make test                               # everything, including the stack
make tf-check                           # terraform fmt/validate/tflint/trivy
make tf-fmt                             # format Terraform
make actions-lint                       # lint workflows (this repo + service repos)
make shellcheck                         # lint shell scripts
make perf                               # benchmark (stack up)
make act REPO=claude JOB=lint           # run a workflow locally
make contract-check                     # is gateway/docs/contract.md current?
make lock                               # re-resolve gateway/uv.lock
```

## AWS

Nothing in the Makefile talks to AWS. The manual commands are in `docs/aws-runbook.md`:
bootstrap, platform, operator keys, portal admin, pause, kill-switch test and final
cleanup. CD does the rest:

| Workflow | When |
|---|---|
| `plan-apply` | plan on pull requests; apply the platform, then deploy the gateway, on `main` |
| each service's `deploy` | after its CI passes on `main` |
| `live-smoke` | nightly, against the live site |
| `nuke` | daily check: destroys everything once `NUKE_AFTER` passes or the budget is spent; or by hand |
| `integration` | by hand: the whole stack on a runner, a commit per service |

## Where things are

| Need | Look at |
|---|---|
| Gateway configuration (env vars) | `gateway/README.md`, "Configuration" |
| Prod gateway settings | `gateway/deploy/main.tf` |
| Local settings | `.env` (from `.env.example`) |
| Manual AWS commands | `docs/aws-runbook.md` |
| Logs on AWS | CloudWatch `/zip-platform/prod/<service>`; Logs Insights `filter request_id = "<id>"` |
| Credentials and sign-in | `docs/auth.md` |
| Costs and the budget | `docs/costs.md` |
