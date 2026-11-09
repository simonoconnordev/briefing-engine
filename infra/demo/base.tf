# What infra/base made: the domain, the image repository, the certificate, SES, secrets.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "terraform_remote_state" "base" {
  backend = "s3"
  config = {
    bucket = "briefing-engine-tfstate-${data.aws_caller_identity.current.account_id}"
    key    = "base/terraform.tfstate"
    region = "eu-west-1"
  }
}

locals {
  base       = data.terraform_remote_state.base.outputs
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  name       = "briefing-engine"
  image      = "${local.base.ecr_repository_url}:${var.image_tag}"
}
