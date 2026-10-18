# The gateway as an ECS service on the shared platform (terraform/prod, in this repository).
# Everything shared (network, load balancer, cluster, secret) is read from the platform's
# SSM parameters; this stack owns only the gateway's own resources.

variable "region" {
  type = string
}

variable "image_tag" {
  description = "Git commit of the image to run (pushed to ECR by the deploy workflow)."
  type        = string
}

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "platform" {
  for_each = toset([
    "cluster_arn", "private_subnet_ids", "gateway_security_group_id",
    "gateway_target_group_arn", "gateway_secret_arn", "domain_name",
    "cognito_user_pool_id", "cognito_user_pool_arn", "cognito_client_id", "cognito_domain",
    "cognito_secret_arn", "database_host", "database_port", "database_name", "database_user",
    "database_secret_arn",
  ])
  name = "/zip-platform/prod/${each.key}"
}

locals {
  name       = "gateway"
  platform   = { for key, parameter in data.aws_ssm_parameter.platform : key => parameter.value }
  repository = "zip-platform/${local.name}"
  registry   = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  secret     = local.platform["gateway_secret_arn"]
  cognito    = local.platform["cognito_secret_arn"]
  database   = local.platform["database_secret_arn"]
}

# What the running gateway may call: user administration in the platform's user pool
# (invite codes create accounts; admins disable them). Nothing else.
data "aws_iam_policy_document" "task" {
  statement {
    sid = "ManageUsers"
    actions = [
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminSetUserPassword", # the password chosen on /account/join
      "cognito-idp:AdminDeleteUser",      # undo a create whose password was refused
      "cognito-idp:AdminDisableUser",
      "cognito-idp:AdminUserGlobalSignOut",
      "cognito-idp:AdminGetUser",           # still exists and enabled? (once a minute)
      "cognito-idp:AdminListGroupsForUser", # still an admin?
    ]
    resources = [local.platform["cognito_user_pool_arn"]]
  }
}

module "service" {
  # The module in the same commit: the gateway lives in the platform repository, so a
  # module change and the gateway change that needs it land (and deploy) together.
  source = "../../terraform/modules/ecs-service"

  name               = local.name
  image              = "${local.registry}/${local.repository}:${var.image_tag}"
  ecr_repository_arn = "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${local.repository}"

  # The public entry point: on-demand capacity, so it is never reclaimed like Spot.
  capacity      = "on_demand"
  cpu           = 256
  memory        = 512
  desired_count = 1

  cluster_arn        = local.platform["cluster_arn"]
  subnet_ids         = split(",", local.platform["private_subnet_ids"])
  security_group_ids = [local.platform["gateway_security_group_id"]]
  target_group_arn   = local.platform["gateway_target_group_arn"]

  environment_variables = {
    PORT      = "8000"
    LOG_LEVEL = "INFO"
    # Cloud Map names. dsh is routed before it is deployed: /dsh returns 502 until then.
    GATEWAY_UPSTREAMS = join(",", [
      for service in ["claude", "grok", "dsh"] : "${service}=http://${service}.zip.internal:8000"
    ])
    # Within the module's 120 s stopTimeout, so in-flight streams can finish on deploy.
    SHUTDOWN_GRACE_SECONDS = "100"

    # Sign-in with Cognito, and invite codes (gateway README, "Accounts and sign-in").
    GATEWAY_PUBLIC_URL           = "https://${local.platform["domain_name"]}"
    GATEWAY_COGNITO_DOMAIN       = local.platform["cognito_domain"]
    GATEWAY_COGNITO_CLIENT_ID    = local.platform["cognito_client_id"]
    GATEWAY_COGNITO_USER_POOL_ID = local.platform["cognito_user_pool_id"]
    # Operators sign in with their Cognito admin account; operator keys (the smoke key
    # among them) are for the API only.
    GATEWAY_ALLOW_KEY_LOGIN = "false"

    # The accounts database (Aurora Serverless v2). Migrations run at startup.
    GATEWAY_DB_HOST = local.platform["database_host"]
    GATEWAY_DB_PORT = local.platform["database_port"]
    GATEWAY_DB_NAME = local.platform["database_name"]
    GATEWAY_DB_USER = local.platform["database_user"]
  }
  secrets = {
    GATEWAY_API_KEYS              = "${local.secret}:GATEWAY_API_KEYS::"
    GATEWAY_SESSION_SECRET        = "${local.secret}:GATEWAY_SESSION_SECRET::"
    GATEWAY_COGNITO_CLIENT_SECRET = "${local.cognito}:client_secret::"
    GATEWAY_DB_PASSWORD           = "${local.database}:password::"
  }
  secret_arns = [local.secret, local.cognito, local.database]

  task_role_policy_json = data.aws_iam_policy_document.task.json
}

output "service" {
  value = module.service.service_name
}
