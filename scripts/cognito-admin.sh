#!/usr/bin/env bash
# Create an admin account in a Cognito pool: Cognito emails a temporary password, and the
# account joins the "admin" group (invite codes, users and keys in the portal).
#
#   scripts/cognito-admin.sh dev  you@example.com   the local fake-cognito (make up first)
#   scripts/cognito-admin.sh prod you@example.com   the pool from terraform/prod
#
# Safe to re-run: an existing account is just added to the group.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-docker}"
aws() { "$ROOT/scripts/aws.sh" "$@"; }
pool="${1:?usage: cognito-admin.sh dev|prod <email>}"
email="${2:?usage: cognito-admin.sh dev|prod <email>}"
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "not an email: $email" >&2; exit 1; }
email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"

case "$pool" in
  dev)
    # The same AWS CLI calls, against fake-cognito on the compose network.
    pool_id="eu-west-2_LocalDev1"
    aws() { (cd "$ROOT" && "$ENGINE" compose --profile tools run --rm -T aws-cli "$@"); } ;;
  prod)
    pool_id="$(aws ssm get-parameter --name /zip-platform/prod/cognito_user_pool_id \
      --query Parameter.Value --output text </dev/null | tr -d '\r')" ;;
  *) echo "usage: cognito-admin.sh dev|prod <email>" >&2; exit 1 ;;
esac

# An existing account is only promoted if its email was proven by Cognito (an account this
# script created). Anyone with an invite code can join under any address, unverified: an
# account like that may belong to someone else, so it is refused unless FORCE=1.
if existing="$(aws cognito-idp admin-get-user --user-pool-id "$pool_id" --username "$email" \
     --query "UserAttributes[?Name=='email_verified' || Name=='custom:invite_id'].[Name,Value]" \
     --output text 2>/dev/null </dev/null)"; then
  verified="$(printf '%s\n' "$existing" | awk '$1 == "email_verified" {print $2}' | tr -d '\r')"
  invite="$(printf '%s\n' "$existing" | awk '$1 == "custom:invite_id" {print $2}' | tr -d '\r')"
  if { [ "$verified" != "true" ] || [ -n "$invite" ]; } && [ -z "${FORCE:-}" ]; then
    cat >&2 <<MSG
== Refusing: $email already has an account that was not created by this script
   (email verified: ${verified:-no}; joined with invite: ${invite:-no}).
   Whoever joined with an invite code chose that address without proving they own it,
   so making it an admin could hand admin rights to someone else. Check who it is on
   Admin -> Users. To promote it anyway: FORCE=1 make ${pool/prod/aws}-admin EMAIL=$email
MSG
    exit 1
  fi
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
if [ "$pool" = dev ]; then
  echo "== The temporary password is in fake-cognito's outbox: http://localhost:${FAKE_COGNITO_PORT:-9229}/outbox"
fi
