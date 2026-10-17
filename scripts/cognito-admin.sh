#!/usr/bin/env bash
# Create an admin account in a Cognito pool: Cognito emails a temporary password, and the
# account joins the "admin" group (invite links, users and keys in the portal).
#
#   scripts/cognito-admin.sh dev  you@example.com   the pool from terraform/dev
#   scripts/cognito-admin.sh prod you@example.com   the pool from terraform/prod
#
# Safe to re-run: an existing account is just added to the group.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
aws() { "$ROOT/scripts/aws.sh" "$@"; }
pool="${1:?usage: cognito-admin.sh dev|prod <email>}"
email="${2:?usage: cognito-admin.sh dev|prod <email>}"
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "not an email: $email" >&2; exit 1; }
email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"

case "$pool" in
  dev)
    pool_id="$(TF_NO_BACKEND_CONFIG=1 "$ROOT/scripts/tf.sh" terraform/dev output -raw user_pool_id </dev/null | tr -d '\r')" ;;
  prod)
    pool_id="$(aws ssm get-parameter --name /zip-platform/prod/cognito_user_pool_id \
      --query Parameter.Value --output text </dev/null | tr -d '\r')" ;;
  *) echo "usage: cognito-admin.sh dev|prod <email>" >&2; exit 1 ;;
esac

if aws cognito-idp admin-get-user --user-pool-id "$pool_id" --username "$email" >/dev/null 2>&1 </dev/null; then
  echo "== $email already has an account"
else
  aws cognito-idp admin-create-user --user-pool-id "$pool_id" --username "$email" \
    --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
    --desired-delivery-mediums EMAIL >/dev/null </dev/null
  echo "== created $email: Cognito is emailing a temporary password"
fi
aws cognito-idp admin-add-user-to-group --user-pool-id "$pool_id" --username "$email" \
  --group-name admin </dev/null
echo "== $email is an admin of $pool_id. Sign in to the portal; it asks for a new password the first time."
