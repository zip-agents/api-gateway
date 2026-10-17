variable "name" {
  description = "Service name: matches its ECR repository, its Cloud Map name and its route on the gateway."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,30}$", var.name))
    error_message = "name must be lowercase letters, digits and hyphens."
  }
}

variable "project" {
  type    = string
  default = "zip-platform"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "image" {
  description = "Full image reference, normally <registry>/zip-platform/<name>:<git sha>."
  type        = string
}

variable "ecr_repository_arn" {
  description = "The repository the image comes from; the execution role may pull from it only."
  type        = string
}

variable "cpu" {
  type    = number
  default = 256
}

variable "memory" {
  type    = number
  default = 512
}

variable "desired_count" {
  description = <<-EOT
    Tasks when running. Fixed: there is no autoscaling, so compute cost has a known ceiling.
    Only applied when the service is created; afterwards `make pause`/`make resume` and the
    kill switch own the running count (see lifecycle.ignore_changes).
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 2
    error_message = "desired_count is capped at 2 to keep costs bounded."
  }
}

variable "capacity" {
  description = "\"spot\" (Fargate Spot, ~70% cheaper, may be reclaimed with 2 minutes' notice) or \"on_demand\"."
  type        = string
  default     = "spot"

  validation {
    condition     = contains(["spot", "on_demand"], var.capacity)
    error_message = "capacity must be \"spot\" or \"on_demand\"."
  }
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "health_check_path" {
  type    = string
  default = "/health/ready"
}

variable "environment_variables" {
  description = "Plain environment variables for the container."
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "Environment variable name -> Secrets Manager reference (\"<secret arn>:<json key>::\")."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Secret ARNs the execution role may read (the secrets above refer to these)."
  type        = list(string)
  default     = []
}

variable "cluster_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "cloud_map_namespace_id" {
  description = "Register as <name>.<namespace> for service discovery (zip services). Null for none."
  type        = string
  default     = null
}

variable "target_group_arn" {
  description = "Attach to this load balancer target group (the gateway). Null for none."
  type        = string
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "stop_timeout_seconds" {
  description = "Time between SIGTERM and SIGKILL. 120 is Fargate's maximum; the services drain streams in it."
  type        = number
  default     = 120
}

variable "task_role_policy_json" {
  description = "IAM policy for the running code, if it calls AWS APIs. Null (the default) means no task role."
  type        = string
  default     = null
}
