# Registers (or removes) a Windows scheduled task that runs scripts\run-daily.ps1 every day.
# Run it yourself; nothing in the app calls it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-daily.ps1            # 06:00
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-daily.ps1 -At 05:30
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-daily.ps1 -Remove
#
# The task runs as you, only while you're signed in, so it needs no stored password. If
# the PC is asleep or off at that time, it runs as soon as it can afterwards, and
# `run-daily` builds that day's briefing then. Re-running a day is safe: it makes no model
# calls and sends no second email.

param(
    [string] $At = '06:00',
    [string] $Name = 'briefing-engine daily run',
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    "Removed the scheduled task '$Name'."
    return
}

$script = Join-Path $PSScriptRoot 'run-daily.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Builds and sends the daily briefing (briefing-engine).' -Force |
    Out-Null
"Scheduled '$Name' daily at $At. Logs go to logs\run-daily-<date>.log."
"Run it now with: Start-ScheduledTask -TaskName '$Name'"
