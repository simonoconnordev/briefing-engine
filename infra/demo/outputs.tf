# The scripts read these to run one-off tasks and to print where the demo is.

output "api_url" {
  value = "https://${local.base.api_hostname}"
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "admin_task_definition" {
  value = aws_ecs_task_definition.task["admin"].arn
}

output "task_subnets" {
  value = aws_subnet.public[*].id
}

output "admin_security_group" {
  value = aws_security_group.worker.id
}

output "log_groups" {
  value = { for k, g in aws_cloudwatch_log_group.task : k => g.name }
}
