# State lives in the bucket this root creates (state.tf). The bucket name includes the
# account ID, so it is passed at init time rather than written here:
#   tf.ps1 does: terraform init -backend-config="bucket=briefing-engine-tfstate-<account>"
# The very first apply runs with local state; bootstrap.ps1 then moves it into the bucket.

terraform {
  backend "s3" {
    key          = "base/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
