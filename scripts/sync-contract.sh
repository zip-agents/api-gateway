#!/usr/bin/env bash
# Build the gateway's copy of the API contract (served at /docs/pages/contract):
# Part 2 of PROMPT.md (contract 1.0.0) plus contract/14-documentation.md (§14, 1.1.0).
# Each service's own docs are fetched from the service at runtime, never copied.
#
#   scripts/sync-contract.sh            refresh the copy
#   scripts/sync-contract.sh --check    exit 1 if the copy is stale (run in CI)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$ROOT/gateway/docs/contract.md"

# PROMPT.md is working material, kept out of git: without it (as in CI) the committed
# copy is the contract, and there is nothing to compare it with.
if [ ! -f "$ROOT/PROMPT.md" ]; then
  if [ "${1:-}" = "--check" ]; then
    echo "PROMPT.md is not here (it is not committed): skipping the contract check"
    exit 0
  fi
  echo "PROMPT.md is missing: put it in the repository root to rebuild the contract copy" >&2
  exit 1
fi
generated="$(mktemp)"
trap 'rm -f "$generated"' EXIT

{
  echo "> Assembled by scripts/sync-contract.sh from PROMPT.md (Part 2) and"
  echo "> contract/14-documentation.md (§14). Edit those, not this copy."
  echo
  sed -n '/^# Part 2: API and behaviour contract/,$p' "$ROOT/PROMPT.md"
  echo
  # Without its leading HTML comment, which the portal would show as text.
  sed '/^<!--/,/-->$/d' "$ROOT/contract/14-documentation.md"
} >"$generated"

if [ "${1:-}" = "--check" ]; then
  if ! cmp -s "$generated" "$TARGET"; then
    echo "gateway/docs/contract.md is stale: run scripts/sync-contract.sh and commit it." >&2
    exit 1
  fi
  echo "contract copy is up to date"
  exit 0
fi

cp "$generated" "$TARGET"
echo "updated gateway/docs/contract.md"
