#!/usr/bin/env bash
# Generate gateway API keys and write them to Secrets Manager. Prints each key once.
#
#   scripts/api-keys.sh smoke alice bob
#
# Replaces the whole key list (removing a key also ends its docs sessions) and generates
# a new session secret. The gateway reads the secret when a task starts, so the script
# then restarts the gateway service.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
aws() { "$ROOT/scripts/aws.sh" "$@"; }
[ "$#" -gt 0 ] || { echo "usage: api-keys.sh <name> [<name>...]" >&2; exit 1; }

random_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

pairs=()
for name in "$@"; do
  [[ "$name" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "invalid key name: $name" >&2; exit 1; }
  key="$(random_hex 24)"
  pairs+=("$name:$key")
  printf '%-12s %s\n' "$name" "$key"
done
keys="$(IFS=,; echo "${pairs[*]}")"
secret="{\"GATEWAY_API_KEYS\":\"$keys\",\"GATEWAY_SESSION_SECRET\":\"$(random_hex 32)\"}"

aws secretsmanager put-secret-value --secret-id zip-platform/prod/gateway \
  --secret-string "$secret" >/dev/null
echo "== stored in zip-platform/prod/gateway"

if aws ecs describe-services --cluster zip-platform-prod --services gateway \
     --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service --cluster zip-platform-prod --service gateway \
    --force-new-deployment >/dev/null
  echo "== gateway restarting to pick up the new keys"
fi
echo "Keep these keys: they are not shown again. Put the smoke key in each repo's SMOKE_API_KEY secret."
