#Requires -Version 7
<#
.SYNOPSIS
  First-time setup of infra/base: creates the state bucket and everything else in base,
  then moves base's own state into that bucket. Run it once.

  Before running:
    - aws login --profile briefing-admin
    - the domain is bought at Cloudflare, and CLOUDFLARE_API_TOKEN is in .env
    - infra/base/terraform.tfvars exists (copy terraform.tfvars.example)

  The bucket can't hold the state of the apply that creates it, so the first apply uses
  local state with the S3 backend switched off; then `terraform init -migrate-state` copies
  that state into the bucket and the local file is deleted.
#>

. "$PSScriptRoot\common.ps1"

Use-AwsSession
Use-CloudflareToken

$dir = Join-Path $RepoRoot 'infra\base'
if (-not (Test-Path (Join-Path $dir 'terraform.tfvars'))) {
    throw 'infra/base/terraform.tfvars is missing. Copy terraform.tfvars.example and fill it in.'
}

$backend = Join-Path $dir 'backend.tf'
$parked = Join-Path $dir 'backend.tf.bootstrapping'
$bucket = Get-StateBucket

$exists = $true
& aws s3api head-bucket --bucket $bucket 2>$null
if ($LASTEXITCODE -ne 0) { $exists = $false }
if ($exists) {
    Write-Host "State bucket $bucket already exists; bootstrap has run. Use tf.ps1 base ..."
    exit 0
}

Write-Host "1/3  Applying infra/base with local state (creates $bucket)..."
Move-Item $backend $parked
try {
    Remove-Item (Join-Path $dir '.terraform') -Recurse -Force -ErrorAction SilentlyContinue
    Invoke-Native terraform "-chdir=$dir" init -input=false
    Invoke-Native terraform "-chdir=$dir" apply
} finally {
    Move-Item $parked $backend -Force
}

Write-Host "2/3  Moving the state into s3://$bucket/base/terraform.tfstate ..."
Invoke-Native terraform "-chdir=$dir" init -input=false -migrate-state -force-copy "-backend-config=bucket=$bucket"

Write-Host '3/3  Removing the local state copies...'
Remove-Item (Join-Path $dir 'terraform.tfstate*') -Force -ErrorAction SilentlyContinue

Write-Host 'Done. Next: .\infra\scripts\put-secrets.ps1'
