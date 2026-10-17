output "state_bucket" {
  description = "Terraform state for every stack, platform and services alike."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arns" {
  description = "Assumed by each repository's deploy job (GitHub environment prod)."
  value       = { for name in keys(var.github_repositories) : name => aws_iam_role.github["${name}-deploy"].arn }
}

output "plan_role_arns" {
  description = "Assumed by each repository's pull-request plan job (read-only)."
  value       = { for name in keys(var.github_repositories) : name => aws_iam_role.github["${name}-plan"].arn }
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}

output "killswitch_function" {
  value = aws_lambda_function.killswitch.function_name
}
