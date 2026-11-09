# The API's TLS certificate. It's free and takes a few minutes to validate, so it lives
# here rather than in demo/: a demo comes up without waiting for DNS validation.

locals {
  api_hostname = "${var.api_subdomain}.${var.domain}"
}

resource "aws_acm_certificate" "api" {
  domain_name       = local.api_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_dns_record" "api_cert_validation" {
  for_each = {
    for o in aws_acm_certificate.api.domain_validation_options : o.domain_name => o
  }
  zone_id = local.zone_id
  name    = trimsuffix(each.value.resource_record_name, ".")
  type    = each.value.resource_record_type
  content = trimsuffix(each.value.resource_record_value, ".")
  ttl     = 1
  proxied = false
  comment = "ACM validation for the API certificate (Terraform, infra/base)"
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for r in cloudflare_dns_record.api_cert_validation : r.name]
}
