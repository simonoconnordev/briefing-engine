# Application secrets, as SSM Parameter Store SecureStrings (free, unlike Secrets Manager).
# Terraform creates each parameter with a placeholder and then never touches its value, so
# the real secret is never in Terraform state: infra/scripts/put-secrets.ps1 sets it.
# The database password is not here: RDS manages it in Secrets Manager (demo/database.tf).

locals {
  parameters = {
    anthropic_api_key = "/briefing-engine/anthropic-api-key"
    cursor_hmac_key   = "/briefing-engine/cursor-hmac-key"
  }
}

resource "aws_ssm_parameter" "app" {
  for_each    = local.parameters
  name        = each.value
  type        = "SecureString"
  value       = "set-me-with-put-secrets.ps1"
  description = "Set by infra/scripts/put-secrets.ps1; Terraform ignores the value."

  lifecycle {
    ignore_changes = [value]
  }
}
