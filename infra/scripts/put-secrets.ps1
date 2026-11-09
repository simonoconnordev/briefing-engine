#Requires -Version 7
<#
.SYNOPSIS
  Copies the app's secrets from .env into SSM Parameter Store (SecureString), without
  printing them. Terraform made the parameters with placeholders and ignores their values.

  ANTHROPIC_API_KEY  must be in .env.
  CURSOR_HMAC_KEY    taken from .env if set there, otherwise a new random key is made.
#>

. "$PSScriptRoot\common.ps1"

Use-AwsSession

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

$cursor = Get-DotEnvValue 'CURSOR_HMAC_KEY'
if (-not $cursor) {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $cursor = [Convert]::ToBase64String($bytes)
}
Set-SecureParameter '/briefing-engine/cursor-hmac-key' $cursor
