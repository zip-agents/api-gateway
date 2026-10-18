#!/usr/bin/env bash
# Create an admin account in the local fake-cognito (make up first): it "emails" a
# temporary password to its outbox, and the account joins the "admin" group (invite codes,
# users and keys in the portal). The AWS CLI calls are the same ones docs/aws-runbook.md
# runs by hand against the real pool.
#
#   scripts/cognito-admin.sh dev you@example.com
#
# Safe to re-run: an existing account is just added to the group.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-docker}"
pool="${1:?usage: cognito-admin.sh dev <email>}"
email="${2:?usage: cognito-admin.sh dev <email>}"
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "not an email: $email" >&2; exit 1; }
email="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"

[ "$pool" = dev ] || { echo "usage: cognito-admin.sh dev <email> (prod: docs/aws-runbook.md)" >&2; exit 1; }
# The AWS CLI, against fake-cognito on the compose network.
pool_id="eu-west-2_LocalDev1"
aws() { (cd "$ROOT" && "$ENGINE" compose --profile tools run --rm -T aws-cli "$@"); }

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
   Admin -> Users. To promote it anyway: FORCE=1 make dev-admin EMAIL=$email
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
echo "== The temporary password is in fake-cognito's outbox: http://localhost:${FAKE_COGNITO_PORT:-9229}/outbox"
