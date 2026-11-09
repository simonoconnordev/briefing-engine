# infra/base: the parts that stay between demos. Together they cost cents a month.
# Applied by hand, from my machine, with infra/scripts/tf.ps1 (bootstrap.ps1 the first time).

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "briefing-engine"
      ManagedBy = "terraform"
      Root      = "base"
    }
  }
}

# Reads CLOUDFLARE_API_TOKEN from the environment (tf.ps1 loads it from .env). The token
# only needs "DNS: Edit" on this one zone.
provider "cloudflare" {}
