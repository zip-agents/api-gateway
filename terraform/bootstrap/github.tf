# GitHub Actions -> AWS with short-lived OIDC credentials; no stored keys anywhere.
#
# Each repository gets two roles:
#   <name>-plan    pull requests: read-only, for `terraform plan -lock=false`
#   <name>-deploy  pushes to main, only in the repository's protected `prod` environment
#
# Service deploy roles can only touch their own service: their ECR repository, their state
# key, and ECS/IAM/log resources named after them. The platform role manages the shared
# stack and so has broad (PowerUser) rights; only the platform repository can assume it.
#
# The gateway lives in the platform repository (github_repositories.gateway names it), so
# its narrow roles trust that repository too. Each job assumes the role for its stack:
# the gateway deploy job gets only gateway rights, never the platform's PowerUser role.

locals {
  github_oidc_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust" {
  for_each = {
    for pair in setproduct(keys(var.github_repositories), ["plan", "deploy"]) :
    "${pair[0]}-${pair[1]}" => { name = pair[0], kind = pair[1] }
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        each.value.kind == "deploy"
        ? "repo:${var.github_repositories[each.value.name]}:environment:prod"
        : "repo:${var.github_repositories[each.value.name]}:pull_request"
      ]
    }
  }
}

resource "aws_iam_role" "github" {
  for_each             = data.aws_iam_policy_document.trust
  name                 = "${local.project}-${each.key}"
  assume_role_policy   = each.value.json
  max_session_duration = 3600
}

# --- plan roles: read everything, change nothing (plans run with -lock=false)
resource "aws_iam_role_policy_attachment" "plan" {
  for_each   = { for name in keys(var.github_repositories) : name => aws_iam_role.github["${name}-plan"].name }
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# --- platform deploy role
resource "aws_iam_role_policy_attachment" "platform_deploy" {
  role       = aws_iam_role.github["platform-deploy"].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# --- service deploy roles
data "aws_iam_policy_document" "service_deploy" {
  for_each = local.services

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPushOwnRepository"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.service[each.key].arn]
  }
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      # Its own state, plus the empty workspace prefix Terraform lists during init.
      values = ["services/${each.key}/*", "env:/*", "env:"]
    }
  }
  statement {
    sid       = "StateOwnKey"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/services/${each.key}/*"]
  }
  statement {
    sid       = "PlatformParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/${local.project}/${local.environment}/*"]
  }
  statement {
    sid = "Unscoped"
    actions = [
      "ssm:DescribeParameters", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
      "ec2:DescribeVpcs", "ec2:DescribeNetworkInterfaces", "elasticloadbalancing:Describe*",
      "ecs:DescribeClusters", "ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition",
      "ecs:DeregisterTaskDefinition", "ecs:ListTaskDefinitions", "logs:DescribeLogGroups",
      "servicediscovery:Get*", "servicediscovery:List*",
    ]
    resources = ["*"] # these actions do not support resource-level permissions
  }
  statement {
    sid = "OwnEcsService"
    actions = [
      "ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService", "ecs:DescribeServices",
      "ecs:TagResource", "ecs:UntagResource", "ecs:ListTagsForResource",
    ]
    resources = [
      "arn:aws:ecs:${var.region}:${local.account_id}:service/${local.cluster_name}/${each.key}",
      "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${local.cluster_name}-${each.key}:*",
    ]
  }
  statement {
    sid = "OwnTaskRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
      "iam:UpdateAssumeRolePolicy", "iam:PassRole",
    ]
    resources = ["arn:aws:iam::${local.account_id}:role/${local.cluster_name}-${each.key}-*"]
  }
  statement {
    sid = "OwnLogGroup"
    actions = [
      "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy",
      "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource",
      "logs:ListTagsLogGroup", "logs:TagLogGroup",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/${local.project}/${local.environment}/${each.key}*"]
  }
  statement {
    sid = "ServiceDiscovery"
    actions = [
      "servicediscovery:CreateService", "servicediscovery:UpdateService",
      "servicediscovery:DeleteService", "servicediscovery:TagResource",
      "servicediscovery:UntagResource",
    ]
    resources = ["*"] # Cloud Map service IDs are generated; scoped to Cloud Map only
  }
}

resource "aws_iam_role_policy" "service_deploy" {
  for_each = data.aws_iam_policy_document.service_deploy
  name     = "deploy-${each.key}"
  role     = aws_iam_role.github["${each.key}-deploy"].id
  policy   = each.value.json
}
