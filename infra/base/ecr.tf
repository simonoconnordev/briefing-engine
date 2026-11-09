# The one image (Dockerfile) that runs as the API, the worker and one-off tasks.
# It lives here, not in demo/, so pushed images survive `terraform destroy` of a demo.

resource "aws_ecr_repository" "app" {
  name                 = "briefing-engine"
  image_tag_mutability = "IMMUTABLE" # tags are git SHAs; one tag, one image, forever

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
