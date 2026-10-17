# Gateway tasks. Everything runs inside containers built from ./Dockerfile; the host
# needs only Docker (or ENGINE=podman). Tests, lint and type checks run in the `dev`
# stage image, which contains the locked dev dependencies and a copy of the code.
#
#   make test        unit tests
#   make check       lint, format check, types and tests: what CI runs
#   make coverage    tests with a coverage report

ENGINE ?= docker
DEV_IMAGE ?= zip-gateway:dev
IMAGE ?= zip-gateway:local
UV_IMAGE ?= ghcr.io/astral-sh/uv:0.13.1-python3.12-trixie-slim
# Containers that write into this directory (lock, fmt) run as the invoking user. A rootless
# engine (rootless Docker or Podman) already maps container root to you, and --user would
# pick an unmapped uid that cannot write the bind mount, so skip it there.
ROOTLESS := $(shell $(ENGINE) info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && echo 1)
AS_ME := $(if $(ROOTLESS),,--user $(shell id -u):$(shell id -g)) -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache
# Extra pytest arguments, e.g. make test PYTEST_ARGS="-k proxy -x"
PYTEST_ARGS ?=

RUN_DEV = $(ENGINE) run --rm $(DEV_IMAGE)

.DEFAULT_GOAL := help
.PHONY: help dev-image test lint typecheck coverage check fmt lock image test-postgres

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

dev-image: ## Build the dev image (code is copied in, so rebuild after every change)
	$(ENGINE) build --target dev -t $(DEV_IMAGE) .

test: dev-image ## Unit tests
	$(RUN_DEV) pytest $(PYTEST_ARGS)

lint: dev-image ## Ruff lint and format check
	$(RUN_DEV) sh -c "ruff check . && ruff format --check ."

typecheck: dev-image ## Mypy (strict)
	$(RUN_DEV) mypy

coverage: dev-image ## Unit tests with a line and branch coverage report
	$(RUN_DEV) sh -c "coverage run -m pytest $(PYTEST_ARGS) && coverage report"

check: dev-image ## Lint, format check, types and tests in one container (CI runs this)
	$(RUN_DEV) sh -c "ruff check . && ruff format --check . && mypy && coverage run -m pytest && coverage report"

test-postgres: dev-image ## Store, account and quota tests against a real Postgres (a throwaway container)
	-$(ENGINE) network create zip-gateway-test >/dev/null 2>&1
	$(ENGINE) run -d --rm --name zip-gateway-postgres --network zip-gateway-test \
	  -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway -e POSTGRES_DB=gateway \
	  postgres:16.14-alpine >/dev/null
	$(ENGINE) exec zip-gateway-postgres sh -c \
	  'for i in $$(seq 1 30); do pg_isready -U gateway -h 127.0.0.1 && exit 0; sleep 1; done; exit 1'
	$(ENGINE) run --rm --network zip-gateway-test \
	  -e GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://gateway:gateway@zip-gateway-postgres:5432/gateway \
	  $(DEV_IMAGE) pytest tests/test_keys.py tests/test_accounts_portal.py tests/test_quota.py -v; status=$$?; \
	  $(ENGINE) rm -f zip-gateway-postgres >/dev/null; $(ENGINE) network rm zip-gateway-test >/dev/null; \
	  exit $$status

fmt: ## Apply ruff formatting and safe fixes to the source tree
	$(ENGINE) run --rm $(AS_ME) -v "$(CURDIR):/app" -w /app \
	  -e UV_PROJECT_ENVIRONMENT=/tmp/venv $(UV_IMAGE) \
	  sh -c "uv run --frozen ruff format . && uv run --frozen ruff check --fix ."

lock: ## Resolve dependencies into uv.lock (needed once before the first build)
	$(ENGINE) run --rm $(AS_ME) -v "$(CURDIR):/app" -w /app $(UV_IMAGE) uv lock

image: ## Build the runtime image
	$(ENGINE) build -t $(IMAGE) .
