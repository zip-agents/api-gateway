variable "region" {
  description = "AWS region for everything in the platform."
  type        = string
}

variable "github_repositories" {
  description = <<-EOT
    GitHub repositories, as "owner/name", keyed by role. "platform" deploys the shared
    infrastructure; every other key is a service of the same name (its ECR repository,
    ECS service and deploy role are named after the key). Two keys may name the same
    repository: the gateway deploys from the platform repository.

    Write each as GitHub writes it in the OIDC token's subject. Organizations using
    GitHub's immutable-ID subjects (zip-agents does) send owner@id/name@id, e.g.
    "zip-agents@344538022/api-gateway@1424863310": the IDs are on
    https://api.github.com/repos/<owner>/<name> ("owner.id" and "id"). A failed login's
    subject is in CloudTrail (AssumeRoleWithWebIdentity, userIdentity.userName).
  EOT
  type        = map(string)

  validation {
    condition     = contains(keys(var.github_repositories), "platform")
    error_message = "github_repositories must include a \"platform\" entry."
  }
  validation {
    condition     = alltrue([for repo in values(var.github_repositories) : can(regex("^[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", repo))])
    error_message = "Each repository must be written as owner/name, or owner@id/name@id."
  }
}

variable "alert_email" {
  description = "Receives budget alerts, anomaly alerts and kill-switch notifications."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly cost limit. Alerts at 50% and 80%; at 100% the kill switch scales every service to zero."
  type        = number
  default     = 100
}

variable "create_github_oidc_provider" {
  description = "False if the account already has the token.actions.githubusercontent.com provider."
  type        = bool
  default     = true
}
