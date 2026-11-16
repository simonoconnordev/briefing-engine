variable "region" {
  description = "AWS region for everything except what must live elsewhere."
  type        = string
  default     = "eu-west-1"
}

variable "domain" {
  description = "The domain bought at Cloudflare, e.g. simonoconnor.dev. Its DNS stays at Cloudflare."
  type        = string
}

variable "api_subdomain" {
  description = "The API is served at <api_subdomain>.<domain>."
  type        = string
  default     = "api"
}

variable "mail_from_subdomain" {
  description = "Custom MAIL FROM domain for SES (bounces go here), as <mail_from_subdomain>.<domain>."
  type        = string
  default     = "mail"
}

variable "ses_verified_recipients" {
  description = <<-EOT
    While SES is in its sandbox it only delivers to verified addresses. Each address listed
    here gets a verification email from AWS when this root is applied.
  EOT
  type        = list(string)
  default     = []
}

variable "dmarc_report_address" {
  description = "Where receivers send DMARC reports (rua). Empty for no reports."
  type        = string
  default     = ""
}

variable "github_oidc_subject" {
  description = <<-EOT
    The start of the `sub` claim in the GitHub OIDC tokens of the repository whose workflows
    may assume the CI roles. This repository uses GitHub's immutable form,
    repo:<owner>@<owner id>/<name>@<repository id>, which names the account and repository by
    their permanent IDs, so a deleted repository re-created under the same name can't inherit
    the roles. Read it with:
      gh api repos/<owner>/<name>/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = "repo:simonoconnordev@195565840/briefing-engine@1460610921"
}
