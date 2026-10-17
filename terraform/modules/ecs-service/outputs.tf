output "service_name" {
  value = aws_ecs_service.service.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.service.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.service.name
}

output "task_role_name" {
  value = one(aws_iam_role.task[*].name)
}
