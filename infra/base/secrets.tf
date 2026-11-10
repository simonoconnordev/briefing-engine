# Application secrets, as SSM Parameter Store SecureStrings (free, unlike Secrets Manager).
# Terraform knows only their names. infra/scripts/put-secrets.ps1 creates them and sets
# their values, so the secrets are never in Terraform state.
#
# Managing them as aws_ssm_parameter resources with `ignore_changes = [value]` is not
# enough: ignore_changes only stops Terraform changing the value, and every refresh still
# reads the decrypted value back into state.
#
# The database password is not here: RDS manages it in Secrets Manager (demo/database.tf).

locals {
  parameters = {
    anthropic_api_key = "/briefing-engine/anthropic-api-key"
    cursor_hmac_key   = "/briefing-engine/cursor-hmac-key"
  }
  # A parameter's ARN is "parameter" followed by its name, which starts with "/".
  parameter_arns = {
    for k, name in local.parameters :
    k => "arn:aws:ssm:${var.region}:${local.account_id}:parameter${name}"
  }
}
