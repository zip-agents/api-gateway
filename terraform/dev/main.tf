# A free Cognito pool for running the gateway locally with real sign-in (`make dev-auth`),
# plus an IAM user whose only permission is managing users in that pool, so the local
# gateway can create accounts from invite links without your admin credentials.

data "aws_caller_identity" "current" {}

locals {
  name = "zip-platform-dev"
}

module "auth" {
  source = "../modules/cognito-pool"

  name          = local.name
  domain_prefix = "zip-platform-dev-${data.aws_caller_identity.current.account_id}"
  site_name     = "zip gateway (dev)"
  login_url     = "${var.local_url}/docs/login"
  callback_urls = ["${var.local_url}/docs/auth/callback"]
  logout_urls   = ["${var.local_url}/docs/login"]
}

resource "aws_iam_user" "gateway" {
  name = "${local.name}-gateway"
}

data "aws_iam_policy_document" "gateway" {
  statement {
    sid = "ManageDevPoolUsers"
    actions = [
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminDisableUser",
      "cognito-idp:AdminUserGlobalSignOut",
    ]
    resources = [module.auth.user_pool_arn]
  }
}

resource "aws_iam_user_policy" "gateway" {
  name   = "cognito-dev-pool"
  user   = aws_iam_user.gateway.name
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_iam_access_key" "gateway" {
  user = aws_iam_user.gateway.name
}
