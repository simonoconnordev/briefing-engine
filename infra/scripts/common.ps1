# Shared by the infra scripts (dot-source it: `. $PSScriptRoot\common.ps1`).
#
# Credentials come from `aws login --profile briefing-admin`: short-lived, from the console
# sign-in, with no access keys on disk. They're exported into this process's environment,
# so Terraform and Docker use the same session. CLOUDFLARE_API_TOKEN comes from .env.
# Nothing here prints a secret.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$script:AwsProfile = if ($env:BRIEFING_AWS_PROFILE) { $env:BRIEFING_AWS_PROFILE } else { 'briefing-admin' }
$script:Region = 'eu-west-1'

function Invoke-Native {
    # Runs a native command and stops on a non-zero exit code.
    param([Parameter(Mandatory)][string]$Exe, [Parameter(ValueFromRemainingArguments)][string[]]$Rest)
    & $Exe @Rest
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Rest -join ' ') failed with exit code $LASTEXITCODE" }
}

function Use-AwsSession {
    $lines = & aws configure export-credentials --profile $script:AwsProfile --format env-no-export 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $lines) {
        throw "No AWS session. Run:  aws login --profile $($script:AwsProfile)"
    }
    foreach ($line in $lines) {
        $name, $value = $line -split '=', 2
        if ($name -in 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_CREDENTIAL_EXPIRATION') {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
    $env:AWS_REGION = $script:Region
    $env:AWS_DEFAULT_REGION = $script:Region
    Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue  # the exported keys win
}

function Get-DotEnvValue {
    param([Parameter(Mandatory)][string]$Name)
    $path = Join-Path $script:RepoRoot '.env'
    if (-not (Test-Path $path)) { return $null }
    foreach ($line in Get-Content $path) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Use-CloudflareToken {
    if (-not $env:CLOUDFLARE_API_TOKEN) {
        $token = Get-DotEnvValue 'CLOUDFLARE_API_TOKEN'
        if (-not $token) {
            throw 'CLOUDFLARE_API_TOKEN is not set. Add it to .env (a token with Zone:Read and DNS:Edit on your zone).'
        }
        $env:CLOUDFLARE_API_TOKEN = $token
    }
}

function Get-AccountId {
    (Invoke-Native aws sts get-caller-identity --query Account --output text).Trim()
}

function Get-StateBucket {
    "briefing-engine-tfstate-$(Get-AccountId)"
}

function Initialize-TerraformRoot {
    # terraform init against the S3 backend, with the bucket name worked out from the account.
    param([Parameter(Mandatory)][ValidateSet('base', 'demo')][string]$Root)
    $dir = Join-Path $script:RepoRoot "infra\$Root"
    Invoke-Native terraform "-chdir=$dir" init -input=false "-backend-config=bucket=$(Get-StateBucket)"
}

function Get-TerraformOutput {
    param([Parameter(Mandatory)][ValidateSet('base', 'demo')][string]$Root, [Parameter(Mandatory)][string]$Name)
    $dir = Join-Path $script:RepoRoot "infra\$Root"
    $json = Invoke-Native terraform "-chdir=$dir" output -json $Name
    $json | ConvertFrom-Json
}
