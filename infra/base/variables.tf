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

variable "github_repository" {
  description = "owner/name of the GitHub repository whose workflows may assume the CI roles."
  type        = string
  default     = "simonoconnordev/briefing-engine"
}
