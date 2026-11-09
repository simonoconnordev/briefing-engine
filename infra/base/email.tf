# Sending email from the domain through Amazon SES: the domain identity with DKIM, a custom
# MAIL FROM subdomain (so SPF aligns with the domain), and DMARC. All the DNS records go to
# Cloudflare, which hosts the domain's DNS.
#
# A new SES account is in the sandbox: it sends only to verified addresses
# (ses_verified_recipients), at most 200 a day. That is enough for one reader. Production
# access is a separate request to AWS, made once bounce and complaint handling exists.

data "cloudflare_zone" "domain" {
  filter = {
    name = var.domain
  }
}

locals {
  zone_id          = data.cloudflare_zone.domain.zone_id
  mail_from_domain = "${var.mail_from_subdomain}.${var.domain}"
}

resource "aws_sesv2_email_identity" "domain" {
  email_identity = var.domain

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

# Easy DKIM: three CNAMEs that point at keys SES rotates for us.
resource "cloudflare_dns_record" "dkim" {
  count   = 3
  zone_id = local.zone_id
  name    = "${aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.domain}"
  type    = "CNAME"
  content = "${aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"
  ttl     = 1 # automatic
  proxied = false
  comment = "SES DKIM (Terraform, infra/base)"
}

resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  email_identity         = aws_sesv2_email_identity.domain.email_identity
  mail_from_domain       = local.mail_from_domain
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

resource "cloudflare_dns_record" "mail_from_mx" {
  zone_id  = local.zone_id
  name     = local.mail_from_domain
  type     = "MX"
  content  = "feedback-smtp.${var.region}.amazonses.com"
  priority = 10
  ttl      = 1
  proxied  = false
  comment  = "SES MAIL FROM (Terraform, infra/base)"
}

resource "cloudflare_dns_record" "mail_from_spf" {
  zone_id = local.zone_id
  name    = local.mail_from_domain
  type    = "TXT"
  content = "\"v=spf1 include:amazonses.com ~all\""
  ttl     = 1
  proxied = false
  comment = "SES MAIL FROM SPF (Terraform, infra/base)"
}

# DMARC starts at p=none: receivers report but don't act, until reports show every
# legitimate message passes. Then it can move to quarantine.
resource "cloudflare_dns_record" "dmarc" {
  zone_id = local.zone_id
  name    = "_dmarc.${var.domain}"
  type    = "TXT"
  content = var.dmarc_report_address == "" ? "\"v=DMARC1; p=none\"" : "\"v=DMARC1; p=none; rua=mailto:${var.dmarc_report_address}\""
  ttl     = 1
  proxied = false
  comment = "DMARC (Terraform, infra/base)"
}

resource "aws_sesv2_email_identity" "recipient" {
  for_each       = toset(var.ses_verified_recipients)
  email_identity = each.value
}
