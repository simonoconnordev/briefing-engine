# CI on GitHub Actions assumes these roles through OIDC: no long-lived AWS keys in GitHub.
#
# The token's `sub` claim depends on how the job runs:
#   a job with `environment: production`  ->  repo:<owner/name>:environment:production
#   a pull_request job                    ->  repo:<owner/name>:pull_request
# The production environment requires my approval and allows only `main`, so that check
# happens in GitHub and can't be skipped by a workflow edit.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_trust" {
  for_each = {
    deploy = "repo:${var.github_repository}:environment:production"
    plan   = "repo:${var.github_repository}:pull_request"
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value]
    }
  }
}

# Deploy: push an image and roll it out to a running demo. It does not run Terraform; the
# demo stack is brought up and down from my machine (demo-up.ps1 / demo-down.ps1).
resource "aws_iam_role" "github_deploy" {
  name                 = "briefing-engine-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["deploy"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid = "EcsRollout"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:RegisterTaskDefinition",
      "ecs:RunTask",
      "ecs:UpdateService",
    ]
    resources = ["*"] # task definition ARNs aren't known in advance; scoped by the condition
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/briefing-engine-ecs-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

# Plan: read-only, for `terraform plan` on pull requests. It may write only the lock file.
resource "aws_iam_role" "github_plan" {
  name                 = "briefing-engine-github-plan"
  assume_role_policy   = data.aws_iam_policy_document.github_trust["plan"].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "github_plan_read_only" {
  role       = aws_iam_role.github_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "github_plan_lock" {
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "github_plan_lock" {
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_plan_lock.json
}

# ReadOnlyAccess includes ssm:Get*, and the default aws/ssm key decrypts for anyone allowed
# to call SSM in the account, so without this a plan job could read the app's secrets.
# Plans don't need them: Terraform knows only the parameter names (secrets.tf).
data "aws_iam_policy_document" "github_plan_no_secrets" {
  statement {
    sid    = "NoAppSecrets"
    effect = "Deny"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "ssm:GetParameterHistory",
    ]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/briefing-engine/*"]
  }
  statement {
    sid       = "NoSecretsManagerValues"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_plan_no_secrets" {
  role   = aws_iam_role.github_plan.id
  policy = data.aws_iam_policy_document.github_plan_no_secrets.json
}
