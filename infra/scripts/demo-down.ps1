#Requires -Version 7
<#
.SYNOPSIS
  Destroys the demo stack: load balancer, ECS, the database (no final snapshot) and the
  network. infra/base (images, certificate, SES, secrets, state) is not touched.
  Terraform lists what it will destroy and asks first (unless -AutoApprove).
#>
param([switch]$AutoApprove)

. "$PSScriptRoot\common.ps1"

Use-AwsSession
Use-CloudflareToken

$dir = Join-Path $RepoRoot 'infra\demo'
if (-not (Test-Path (Join-Path $dir '.terraform\terraform.tfstate'))) {
    Initialize-TerraformRoot demo
}
# image_tag has no default; any value will do for a destroy.
$destroy = @("-chdir=$dir", 'destroy', '-var=image_tag=unused')
if ($AutoApprove) { $destroy += '-auto-approve' }
Invoke-Native terraform @destroy
Write-Host 'The demo is down. Base resources (a few cents a month) remain.'
