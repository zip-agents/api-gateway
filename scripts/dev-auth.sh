#!/usr/bin/env bash
# Create (or update) the dev Cognito pool in terraform/dev, then point the local stack at
# it by writing its settings into .env. Run `make env` first; re-running is safe.
#
#   scripts/dev-auth.sh          apply terraform/dev and update .env
#   scripts/dev-auth.sh destroy  destroy the dev pool and remove its settings from .env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
STACK=terraform/dev
export TF_NO_BACKEND_CONFIG=1 # local state (see terraform/dev/versions.tf)
tf() { "$ROOT/scripts/tf.sh" "$STACK" "$@"; }
output() { tf output -raw "$1" </dev/null | tr -d '\r'; }

KEYS=(GATEWAY_COGNITO_DOMAIN GATEWAY_COGNITO_CLIENT_ID GATEWAY_COGNITO_CLIENT_SECRET
      GATEWAY_COGNITO_USER_POOL_ID GATEWAY_PUBLIC_URL
      GATEWAY_DEV_AWS_ACCESS_KEY_ID GATEWAY_DEV_AWS_SECRET_ACCESS_KEY)

remove_settings() {
  local pattern
  pattern="^($(IFS='|'; echo "${KEYS[*]}"))="
  grep -Ev "$pattern|^# dev Cognito pool" "$ENV_FILE" >"$ENV_FILE.tmp" || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}

tf init -input=false >/dev/null
if [ "${1:-}" = "destroy" ]; then
  tf destroy -auto-approve
  [ ! -f "$ENV_FILE" ] || remove_settings
  echo "== dev pool destroyed; .env is back to API-key sign-in only"
  exit 0
fi
[ -f "$ENV_FILE" ] || { echo "no .env: run 'make env' first" >&2; exit 1; }
tf apply -auto-approve

values=(
  "$(output domain)" "$(output client_id)" "$(output client_secret)"
  "$(output user_pool_id)" "$(output public_url)"
  "$(output access_key_id)" "$(output secret_access_key)"
)
remove_settings
{
  echo "# dev Cognito pool, written by scripts/dev-auth.sh (make dev-auth)"
  for i in "${!KEYS[@]}"; do echo "${KEYS[$i]}=${values[$i]}"; done
} >>"$ENV_FILE"
chmod 600 "$ENV_FILE"
cat <<MSG
== .env now points the local gateway at the dev pool ${values[3]}.
   Next: make up, then make dev-admin EMAIL=<you> to create your admin account.
MSG
