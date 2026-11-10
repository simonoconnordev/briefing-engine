#Requires -Version 7
<#
.SYNOPSIS
  Creates or updates the app's secrets in SSM Parameter Store (SecureString) from .env,
  without printing them. Terraform knows only the parameter names (infra/base/secrets.tf),
  so the values never reach Terraform state.

  ANTHROPIC_API_KEY  must be in .env.
  CURSOR_HMAC_KEY    taken from .env if set there; otherwise a new random key is made the
                     first time, and an existing one is kept on later runs.
#>

. "$PSScriptRoot\common.ps1"

Use-AwsSession

function Test-Parameter([string]$Name) {
    # Asks only whether the parameter exists; no value is fetched.
    $count = (Invoke-Native aws ssm describe-parameters `
            --parameter-filters "Key=Name,Option=Equals,Values=$Name" `
            --query 'length(Parameters)' --output text).Trim()
    $count -ne '0'
}

function Set-SecureParameter([string]$Name, [string]$Value) {
    # The value goes through a temp file, not the command line, so it never shows in a
    # process listing. The file is deleted straight away.
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp.FullName, $Value)
        Invoke-Native aws ssm put-parameter --name $Name --type SecureString --overwrite `
            --value "file://$($tmp.FullName)" --output text --query Version | Out-Null
    } finally {
        Remove-Item $tmp.FullName -Force
    }
    Write-Host "Set $Name"
}

$anthropic = Get-DotEnvValue 'ANTHROPIC_API_KEY'
if (-not $anthropic) { throw 'ANTHROPIC_API_KEY is not set in .env' }
Set-SecureParameter '/briefing-engine/anthropic-api-key' $anthropic

$cursorName = '/briefing-engine/cursor-hmac-key'
$cursor = Get-DotEnvValue 'CURSOR_HMAC_KEY'
if ($cursor) {
    Set-SecureParameter $cursorName $cursor
} elseif (Test-Parameter $cursorName) {
    # A new key would invalidate every pagination cursor already handed out.
    Write-Host "Kept $cursorName (set CURSOR_HMAC_KEY in .env to replace it)"
} else {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    Set-SecureParameter $cursorName ([Convert]::ToBase64String($bytes))
}
