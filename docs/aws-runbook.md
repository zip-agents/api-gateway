# AWS runbook: the manual steps

Everything that changes AWS is done either by CD (the workflows) or by hand, with the
commands below. Nothing in the Makefile talks to AWS. Run them one at a time from the
repository root, and read each one's output before running the next.

All commands assume these two variables, set once per shell:

```bash
export AWS_PROFILE=zip-admin      # the CLI profile you sign in with (step 1)
export AWS_REGION=eu-west-2
```

## 1. Tools and credentials

- AWS CLI v2 (`aws --version` says `aws-cli/2.x`). Remove any old v1 install and any old
  `~/.aws/credentials` entries with long-lived keys first.
- Terraform 1.16.x (`terraform version`), `jq` and `openssl`.
- Sign in, and check who you are:

```bash
aws sso login                     # or however your profile signs in
aws sts get-caller-identity
```

## 2. Bootstrap (once, as an administrator)

The state bucket, the GitHub OIDC roles, ECR, the budget and the kill switch. The bucket
is created by this stack, so the first apply uses local state, which is then moved into
the bucket.

```bash
cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars
# edit it: region, alert email, budget (gitignored: it holds your email)
printf 'terraform {\n  backend "local" {}\n}\n' > terraform/bootstrap/local_override.tf
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply
rm terraform/bootstrap/local_override.tf
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=zip-platform-tfstate-$ACCOUNT-$AWS_REGION
terraform -chdir=terraform/bootstrap init -migrate-state -force-copy \
  -backend-config=bucket=$BUCKET -backend-config=region=$AWS_REGION
rm terraform/bootstrap/terraform.tfstate terraform/bootstrap/terraform.tfstate.backup
```

Then confirm the SNS subscription email AWS sends, so budget alerts reach you.

## 3. The platform (first time by hand; CI applies it afterwards)

```bash
cp terraform/prod/terraform.tfvars.example terraform/prod/terraform.tfvars
# edit it: hosted zone and domain. Commit it: CI reads it, and it holds no secrets.
terraform -chdir=terraform/prod init -backend-config=bucket=$BUCKET -backend-config=region=$AWS_REGION
terraform -chdir=terraform/prod plan
terraform -chdir=terraform/prod apply
```

## 4. Operator API keys

The gateway's secret holds the operator keys (`name:key`, comma-separated) and the session
secret. The gateway won't start until it has a value. First time, with the smoke key:

```bash
SMOKE_KEY=$(openssl rand -hex 24); echo "smoke key: $SMOKE_KEY"
aws secretsmanager put-secret-value --secret-id zip-platform/prod/gateway \
  --secret-string "$(jq -cn --arg k "smoke:$SMOKE_KEY" --arg s "$(openssl rand -hex 32)" \
    '{GATEWAY_API_KEYS: $k, GATEWAY_SESSION_SECRET: $s}')"
```

Put the smoke key in each repository's `SMOKE_API_KEY` secret. To add one more key later,
leaving the others and every session alone:

```bash
NAME=alice; KEY=$(openssl rand -hex 24); echo "$NAME key: $KEY"
CURRENT=$(aws secretsmanager get-secret-value --secret-id zip-platform/prod/gateway \
  --query SecretString --output text)
aws secretsmanager put-secret-value --secret-id zip-platform/prod/gateway \
  --secret-string "$(jq -c --arg k "$NAME:$KEY" '.GATEWAY_API_KEYS += "," + $k' <<<"$CURRENT")"
aws ecs update-service --cluster zip-platform-prod --service gateway --force-new-deployment
```

To remove one, edit `GATEWAY_API_KEYS` the same way with `jq` (drop that `name:key`), put
it back, and restart the gateway. The gateway reads the secret when a task starts, hence
the restart.

## 5. Your portal admin

```bash
POOL=$(aws ssm get-parameter --name /zip-platform/prod/cognito_user_pool_id \
  --query Parameter.Value --output text)
EMAIL=you@example.com
aws cognito-idp admin-create-user --user-pool-id $POOL --username $EMAIL \
  --user-attributes Name=email,Value=$EMAIL Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --username $EMAIL --group-name admin
```

Cognito emails a temporary password. Sign in at `https://<domain>/account/login`.

## 6. Day to day

```bash
# task counts
aws ecs describe-services --cluster zip-platform-prod --services gateway claude grok dsh \
  --query 'services[].[serviceName,desiredCount,runningCount]' --output table
# pause one service (repeat per service); resume with --desired-count 1
aws ecs update-service --cluster zip-platform-prod --service claude --desired-count 0
# fire the budget kill switch by hand (scales everything to 0)
aws lambda invoke --function-name zip-platform-killswitch \
  --cli-binary-format raw-in-base64-out --payload '{"test": "manual"}' /dev/stdout
```

## 7. Switching it off for good

The `nuke` workflow (`.github/workflows/nuke.yml`) does this automatically on the date in
the repository variable `NUKE_AFTER`, or once the month's spend reaches the budget, and it
can be run by hand from the Actions tab. It runs `scripts/nuke.sh`, which leaves only the
bootstrap stack (free to keep), the Route 53 zone ($0.50/month) and the domain.

To remove those too, afterwards:

```bash
terraform -chdir=terraform/bootstrap init -backend-config=bucket=$BUCKET -backend-config=region=$AWS_REGION
printf 'terraform {\n  backend "local" {}\n}\n' > terraform/bootstrap/local_override.tf
terraform -chdir=terraform/bootstrap init -migrate-state -force-copy
terraform -chdir=terraform/bootstrap destroy
rm terraform/bootstrap/local_override.tf terraform/bootstrap/terraform.tfstate*
aws route53 delete-hosted-zone --id $(aws route53 list-hosted-zones-by-name \
  --dns-name <your domain> --query 'HostedZones[0].Id' --output text)
```

Then turn off the domain's auto-renew in the Route 53 console (Registered domains).
