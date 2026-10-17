#!/usr/bin/env bash
# Statically check GitHub Actions workflows with actionlint (which also runs shellcheck on
# every `run:` script and validates expressions, job graphs and action inputs).
# Checks this repository and each service repository. Read-only: each repository is
# mounted read-only, so nothing in it can change.
#
#   scripts/check-actions.sh                 all repositories
#   scripts/check-actions.sh claude grok     just these
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-docker}"
IMAGE="${ACTIONLINT_IMAGE:-rhysd/actionlint:1.7.12}"
if [ "$#" -gt 0 ]; then repos=("$@"); else repos=(. claude grok dsh-qwen3.8); fi

failed=0
for repo in "${repos[@]}"; do
  dir="$ROOT/$repo"
  if [ ! -d "$dir/.github/workflows" ]; then
    echo "-- $repo: no workflows, skipped"
    continue
  fi
  echo "-- $repo"
  if "$ENGINE" run --rm -v "$dir:/repo:ro" -w /repo "$IMAGE" -color; then
    echo "   ok"
  else
    failed=1
  fi
done
exit "$failed"
