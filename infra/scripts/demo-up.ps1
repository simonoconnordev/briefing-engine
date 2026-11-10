#Requires -Version 7
<#
.SYNOPSIS
  Brings the demo up: pushes the current image, applies infra/demo, migrates the database,
  and restarts the services on the migrated schema. About 10-15 minutes, most of it RDS.

  Terraform shows its plan and asks before changing anything (unless -AutoApprove).
  When you're done: .\infra\scripts\demo-down.ps1
#>
param(
    [string]$ImageTag,
    [switch]$AutoApprove
)

. "$PSScriptRoot\common.ps1"

Use-AwsSession
Use-CloudflareToken

# The services read their secrets from SSM at start-up; without them every task fails.
if (-not (Test-Path (Join-Path $RepoRoot 'infra\base\.terraform\terraform.tfstate'))) {
    Initialize-TerraformRoot base
}
$names = (Get-TerraformOutput base parameter_names).PSObject.Properties.Value
$found = [int](Invoke-Native aws ssm describe-parameters `
        --parameter-filters "Key=Name,Option=Equals,Values=$($names -join ',')" `
        --query 'length(Parameters)' --output text).Trim()
if ($found -ne @($names).Count) {
    throw 'The app secrets are not in SSM yet. Run .\infra\scripts\put-secrets.ps1 first.'
}

if (-not $ImageTag) {
    $ImageTag = & "$PSScriptRoot\push-image.ps1" | Select-Object -Last 1
}
Write-Host "Image tag: $ImageTag"

$dir = Join-Path $RepoRoot 'infra\demo'
if (-not (Test-Path (Join-Path $dir '.terraform\terraform.tfstate'))) {
    Initialize-TerraformRoot demo
}
$apply = @("-chdir=$dir", 'apply', "-var=image_tag=$ImageTag")
if ($AutoApprove) { $apply += '-auto-approve' }
Invoke-Native terraform @apply

# The services start alongside the migration; restarting them afterwards means they come up
# on the finished schema rather than relying on their retries.
& "$PSScriptRoot\run-admin.ps1" migrate
$cluster = Get-TerraformOutput demo cluster
foreach ($service in 'api', 'worker') {
    Invoke-Native aws ecs update-service --cluster $cluster --service $service --force-new-deployment `
        --query 'service.serviceName' --output text | Out-Null
}
Write-Host 'Waiting for the services to settle...'
Invoke-Native aws ecs wait services-stable --cluster $cluster --services api worker

$url = Get-TerraformOutput demo api_url
Write-Host "The demo is up: $url/docs   (readiness: $url/readyz)"
Write-Host 'First time? Create a user and an API key with run-admin.ps1 (see infra/README.md).'
