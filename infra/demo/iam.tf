# Three roles, each with only what it needs.
#   execution: what ECS itself uses to start a task (pull the image, write logs, read secrets)
#   api:       what the API's code may call (nothing in AWS)
#   worker:    what the worker's code may call (send email through SES)
# Names start with briefing-engine-ecs- so the CI deploy role may pass them (base/github.tf).

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "AppParameters"
    actions   = ["ssm:GetParameters"]
    resources = values(local.base.parameter_arns)
  }
  statement {
    sid       = "DatabasePassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-ecs-api"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-ecs-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# In the SES sandbox, sending is checked against the recipient's identity as well as the
# sender's, so the resource covers this account's identities in this region.
data "aws_iam_policy_document" "worker_send" {
  statement {
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["arn:aws:ses:${local.region}:${local.account_id}:identity/*"]
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [local.base.mail_from_address]
    }
  }
}

resource "aws_iam_role_policy" "worker_send" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_send.json
}
