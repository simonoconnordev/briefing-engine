# Same bucket as infra/base, different key. tf.ps1 passes the bucket name at init time.

terraform {
  backend "s3" {
    key          = "demo/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
