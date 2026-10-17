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
  EOT
  type        = map(string)

  validation {
    condition     = contains(keys(var.github_repositories), "platform")
    error_message = "github_repositories must include a \"platform\" entry."
  }
  validation {
    condition     = alltrue([for repo in values(var.github_repositories) : can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo))])
    error_message = "Each repository must be written as owner/name."
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
