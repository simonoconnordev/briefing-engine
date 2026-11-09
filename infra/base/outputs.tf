# demo/ reads these through terraform_remote_state.

output "region" {
  value = var.region
}

output "domain" {
  value = var.domain
}

output "cloudflare_zone_id" {
  value = local.zone_id
}

output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "api_hostname" {
  value = local.api_hostname
}

output "api_certificate_arn" {
  # The validated ARN, so demo/ never attaches a certificate that isn't issued yet.
  value = aws_acm_certificate_validation.api.certificate_arn
}

output "ses_identity_arn" {
  value = aws_sesv2_email_identity.domain.arn
}

output "mail_from_address" {
  value = "briefing@${var.domain}"
}

output "parameter_arns" {
  value = { for k, p in aws_ssm_parameter.app : k => p.arn }
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "github_plan_role_arn" {
  value = aws_iam_role.github_plan.arn
}
