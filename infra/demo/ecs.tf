# One image, three task definitions:
#   api     - `briefing api`, behind the load balancer
#   worker  - `briefing worker`, the scheduler and job runner
#   admin   - one-off commands: `briefing migrate` before a rollout, `create-user`, `create-key`
# All of them get the same settings; only the command and the role differ.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # extra CloudWatch cost; the logs and the ledger tables are enough here
  }
}

resource "aws_cloudwatch_log_group" "task" {
  for_each          = toset(["api", "worker", "admin"])
  name              = "/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

locals {
  environment = [
    for k, v in {
      LOG_FORMAT     = "json"
      API_HOST       = "0.0.0.0"
      DB_HOST        = aws_db_instance.main.address
      DB_PORT        = tostring(aws_db_instance.main.port)
      DB_NAME        = aws_db_instance.main.db_name
      DB_USER        = aws_db_instance.main.username
      MAIL_TRANSPORT = "ses"
      AWS_REGION     = local.region
      MAIL_FROM      = "Briefing Engine <${local.base.mail_from_address}>"
      # The marking page (`briefing web`) isn't deployed, so emails carry no marking link.
      WEB_BASE_URL = ""
    } : { name = k, value = v }
  ]

  secrets = [
    { name = "ANTHROPIC_API_KEY", valueFrom = local.base.parameter_arns["anthropic_api_key"] },
    { name = "CURSOR_HMAC_KEY", valueFrom = local.base.parameter_arns["cursor_hmac_key"] },
    # A JSON key inside the RDS-managed secret: <secret ARN>:<key>::
    { name = "DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
  ]

  tasks = {
    api    = { command = ["api"], role = aws_iam_role.api.arn }
    worker = { command = ["worker"], role = aws_iam_role.worker.arn }
    admin  = { command = ["migrate"], role = aws_iam_role.worker.arn }
  }
}

resource "aws_ecs_task_definition" "task" {
  for_each = local.tasks

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = each.value.role

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = each.key
    image       = local.image
    essential   = true
    command     = each.value.command
    environment = local.environment
    secrets     = local.secrets
    portMappings = each.key == "api" ? [{
      containerPort = 8080
      protocol      = "tcp"
    }] : []
    stopTimeout = each.key == "worker" ? 90 : 30 # let the current batch finish (§9)
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task[each.key].name
        awslogs-region        = local.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.task["api"].arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.task["worker"].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = true
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
