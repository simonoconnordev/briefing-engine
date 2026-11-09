# infra/demo: everything that costs money by the hour. `demo-up.ps1` applies it for a demo
# and `demo-down.ps1` destroys it afterwards. About $65/month if left running; a few cents
# for an afternoon.

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
  region = "eu-west-1"

  default_tags {
    tags = {
      Project   = "briefing-engine"
      ManagedBy = "terraform"
      Root      = "demo"
    }
  }
}

provider "cloudflare" {}
