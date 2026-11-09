#Requires -Version 7
<#
.SYNOPSIS
  Runs terraform in infra/base or infra/demo with the AWS session and Cloudflare token set.

.EXAMPLE
  .\infra\scripts\tf.ps1 base plan
  .\infra\scripts\tf.ps1 demo output api_url
#>
param(
    [Parameter(Mandatory, Position = 0)][ValidateSet('base', 'demo')][string]$Root,
    [Parameter(Position = 1, ValueFromRemainingArguments)][string[]]$TerraformArgs
)

. "$PSScriptRoot\common.ps1"

Use-AwsSession
Use-CloudflareToken

$dir = Join-Path $RepoRoot "infra\$Root"
if (-not (Test-Path (Join-Path $dir '.terraform\terraform.tfstate'))) {
    Initialize-TerraformRoot $Root
}
& terraform "-chdir=$dir" @TerraformArgs
exit $LASTEXITCODE
