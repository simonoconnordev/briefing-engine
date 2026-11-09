# HTTPS in front of the API. HTTP only redirects.
# The target group checks /healthz (is the process up?), not /readyz: a database outage
# should make requests fail fast, not make ECS replace every task in a loop (§12.3).

resource "aws_lb" "api" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.name}-api"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.base.api_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# api.<domain> points at the load balancer while the demo is up, and goes when it's down.
resource "cloudflare_dns_record" "api" {
  zone_id = local.base.cloudflare_zone_id
  name    = local.base.api_hostname
  type    = "CNAME"
  content = aws_lb.api.dns_name
  ttl     = 60
  proxied = false # TLS ends at the ALB with the ACM certificate
  comment = "API load balancer (Terraform, infra/demo)"
}
