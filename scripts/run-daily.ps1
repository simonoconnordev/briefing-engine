# Runs one day's briefing: starts Postgres and Mailpit if they aren't running, runs
# `briefing run-daily`, and appends the output to logs\run-daily-<date>.log.
# Task Scheduler runs this (see schedule-daily.ps1); it also works by hand:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-daily.ps1
#
# The exit code is run-daily's, so a failed run shows as failed in Task Scheduler.

# Not 'Stop': Windows PowerShell turns a native command's stderr into an error, and docker
# and briefing both write progress there. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs ("run-daily-{0:yyyy-MM-dd}.log" -f (Get-Date))

function Write-Log([string] $line) {
    "{0:HH:mm:ss} {1}" -f (Get-Date), $line | Tee-Object -FilePath $log -Append
}

# A scheduled task starts with the PATH it was registered with; make sure docker is on it.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

Write-Log 'starting'

# Docker Desktop may still be starting after a reboot or wake-up: give it a few minutes.
$deadline = (Get-Date).AddMinutes(5)
while ($true) {
    docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -eq 0) { break }
    if ((Get-Date) -gt $deadline) {
        Write-Log 'Docker is not running after 5 minutes; is Docker Desktop set to start at sign-in?'
        exit 2
    }
    Start-Sleep -Seconds 10
}

docker compose up -d --wait 2>&1 | ForEach-Object { Write-Log "$_" }
if ($LASTEXITCODE -ne 0) {
    Write-Log "docker compose up failed ($LASTEXITCODE)"
    exit 3
}

& (Join-Path $root '.venv\Scripts\briefing.exe') run-daily 2>&1 | ForEach-Object { Write-Log "$_" }
$code = $LASTEXITCODE
Write-Log "finished with exit code $code"
exit $code
