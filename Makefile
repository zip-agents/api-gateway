# Platform tasks: the local stack, the AWS infrastructure (Terraform), cost controls and CI
# tooling. Everything runs in containers: the host needs Docker (or ENGINE=podman), plus
# `act` for `make act`. The gateway is part of this repository (gateway/). The zip services
# (claude/, grok/, dsh-qwen3.8/) are separate repositories checked out alongside; this
# repository only reads them.
#
# AWS targets (aws-*) use platform.env (AWS_REGION, AWS_PROFILE); see docs/deployment.md.

-include platform.env
export AWS_REGION AWS_PROFILE

ENGINE ?= docker
COMPOSE ?= $(ENGINE) compose
UV_IMAGE ?= ghcr.io/astral-sh/uv:0.13.1-python3.12-trixie-slim
# Run tool containers as the invoking user so files they write are not owned by root. A
# rootless engine (rootless Docker or Podman) already maps container root to you, and
# --user would pick an unmapped uid that cannot write the bind mounts, so skip it there.
ROOTLESS := $(shell $(ENGINE) info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && echo 1)
AS_ME := $(if $(ROOTLESS),,--user $(shell id -u):$(shell id -g)) -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache

.DEFAULT_GOAL := help
.PHONY: help env lock gateway-check gateway-test gateway-fmt test test-unit test-smoke \
        up up-dsh down logs smoke perf contract-sync contract-check actions-lint act ci shellcheck \
        tf-fmt tf-check test-lambda aws-bootstrap aws-plan aws-apply aws-api-keys aws-pause \
        aws-resume aws-status aws-test-killswitch aws-down aws-nuke dev-auth dev-auth-down \
        dev-admin aws-admin

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

env: ## Create .env with freshly generated local secrets (never overwrites)
	@test ! -e .env || { echo ".env already exists; leaving it alone"; exit 0; }; \
	key="$$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')"; \
	secret="$$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"; \
	sed -e "s/^GATEWAY_API_KEYS=.*/GATEWAY_API_KEYS=local:$$key/" \
	    -e "s/^SMOKE_API_KEY=.*/SMOKE_API_KEY=$$key/" \
	    -e "s/^GATEWAY_SESSION_SECRET=.*/GATEWAY_SESSION_SECRET=$$secret/" \
	    .env.example > .env; \
	echo "wrote .env; your API key is $$key"

lock: ## Resolve gateway dependencies into gateway/uv.lock
	$(MAKE) -C gateway lock ENGINE=$(ENGINE)

gateway-test: ## Gateway unit tests, in the gateway dev image
	$(MAKE) -C gateway test ENGINE=$(ENGINE)

gateway-check: ## Gateway lint, types, tests and coverage, in the gateway dev image
	$(MAKE) -C gateway check ENGINE=$(ENGINE)

gateway-fmt: ## Apply ruff formatting and safe fixes to the gateway
	$(MAKE) -C gateway fmt ENGINE=$(ENGINE)

test-unit: gateway-check ## All unit-level checks (containers only)

test-smoke: up smoke ## Start the stack, then smoke-test every service through the gateway

test: test-unit test-smoke ## Everything: unit checks, then the integrated stack

up: ## Build and start the gateway with claude and grok (http://localhost:8080/docs)
	$(COMPOSE) up --build --detach --wait

up-dsh: ## Same, including the dsh service
	$(COMPOSE) --profile dsh up --build --detach --wait

down: ## Stop and remove the stack
	$(COMPOSE) --profile dsh --profile test down --remove-orphans

logs: ## Follow the stack's logs
	$(COMPOSE) logs --follow

smoke: ## Smoke-test every service through the gateway (stack must be up)
	$(COMPOSE) --profile test run --rm --build smoke

perf: ## Benchmark every service through the gateway (stack must be up): throughput, first byte, memory
	scripts/perf.sh

contract-sync: ## Rebuild gateway/docs/contract.md from PROMPT.md and contract/
	scripts/sync-contract.sh

contract-check: ## Fail if gateway/docs/contract.md is stale
	scripts/sync-contract.sh --check

actions-lint: ## Statically check the workflows of this repo and every service repo
	scripts/check-actions.sh

act: ## Run a repo's workflow locally: make act REPO=claude [JOB=lint] [EVENT=pull_request]
	scripts/run-actions.sh "$(or $(REPO),.)" "$(EVENT)" "$(JOB)"

ci: actions-lint shellcheck tf-check test-lambda ## The platform repository's CI checks

shellcheck: ## Lint the shell scripts
	$(ENGINE) run --rm -v "$(CURDIR):/mnt:ro" -w /mnt koalaman/shellcheck:v0.11.0 scripts/*.sh

TERRAFORM_IMAGE ?= hashicorp/terraform:1.16.3
TFLINT_IMAGE ?= ghcr.io/terraform-linters/tflint:v0.64.0
TRIVY_IMAGE ?= aquasec/trivy:0.74.0
TF_RUN = $(ENGINE) run --rm $(AS_ME) -v "$(CURDIR):/work" -w /work
TF_STACKS = terraform/bootstrap terraform/prod terraform/dev terraform/modules/ecs-service \
            terraform/modules/cognito-pool gateway/deploy
# This repository's Terraform only: the zip services' deploy/ stacks are theirs to check.
TF_DIRS = terraform gateway/deploy

tf-fmt: ## Format this repository's Terraform (terraform/ and gateway/deploy)
	$(TF_RUN) --entrypoint sh $(TERRAFORM_IMAGE) -c 'for d in $(TF_DIRS); do terraform fmt -recursive $$d; done'

tf-check: ## Terraform fmt check, validate, tflint and trivy (no AWS credentials needed)
	$(TF_RUN) --entrypoint sh $(TERRAFORM_IMAGE) -c \
	  'set -e; for d in $(TF_DIRS); do terraform fmt -check -recursive $$d; done; \
	   for s in $(TF_STACKS); do echo "== validate $$s"; \
	     terraform -chdir=$$s init -backend=false -input=false >/dev/null; terraform -chdir=$$s validate; done'
	for d in $(TF_DIRS); do $(TF_RUN) $(TFLINT_IMAGE) --chdir=$$d --recursive --config=/work/.tflint.hcl || exit 1; done
	for d in $(TF_DIRS); do $(TF_RUN) $(TRIVY_IMAGE) config --exit-code 1 --severity HIGH,CRITICAL \
	  --ignorefile .trivyignore --cache-dir /tmp/trivy --skip-version-check $$d || exit 1; done

test-lambda: ## Unit tests for the budget kill switch
	$(ENGINE) run --rm $(AS_ME) -v "$(CURDIR)/lambda/killswitch:/src:ro" -w /src \
	  -e PYTHONDONTWRITEBYTECODE=1 python:3.12.15-slim-bookworm python -m unittest -v test_handler

# --------------------------------------------------------------------------- local sign-in

dev-auth: ## Create the free dev Cognito pool (terraform/dev) and point .env at it
	scripts/dev-auth.sh

dev-auth-down: ## Destroy the dev Cognito pool and remove its settings from .env
	scripts/dev-auth.sh destroy

dev-admin: ## Create your admin account in the dev pool: make dev-admin EMAIL=you@example.com
	scripts/cognito-admin.sh dev $(EMAIL)

# --------------------------------------------------------------------------- AWS

aws-bootstrap: ## Once, as an administrator: state bucket, CI roles, ECR, budget and kill switch
	scripts/bootstrap.sh

aws-plan: ## Plan the shared platform (terraform/prod)
	scripts/tf.sh terraform/prod init -input=false
	scripts/tf.sh terraform/prod plan

aws-apply: ## Apply the shared platform (terraform/prod); CI does this on merge to main
	scripts/tf.sh terraform/prod init -input=false
	scripts/tf.sh terraform/prod apply

aws-admin: ## Create a portal admin in the prod pool (Cognito emails a password): make aws-admin EMAIL=you@example.com
	scripts/cognito-admin.sh prod $(EMAIL)

aws-api-keys: ## Generate gateway API keys into Secrets Manager: make aws-api-keys NAMES="smoke alice"
	scripts/api-keys.sh $(or $(NAMES),smoke)

aws-pause: ## Scale every service to 0 (compute and data transfer stop; ~$$2/day fixed remains)
	scripts/scale.sh pause

aws-resume: ## Scale every service back to its configured count
	scripts/scale.sh resume

aws-status: ## Desired and running task counts
	scripts/scale.sh status

aws-test-killswitch: ## Invoke the budget kill switch now (scales everything to 0; undo with aws-resume)
	scripts/aws.sh lambda invoke --function-name zip-platform-killswitch \
	  --cli-binary-format raw-in-base64-out --payload '{"test": "manual invocation"}' /dev/stdout

aws-down: ## Destroy every service and the shared platform (keeps bootstrap: ~$$1/month)
	scripts/teardown.sh down

aws-nuke: ## Destroy everything, bootstrap included (after the interview)
	scripts/teardown.sh nuke
