#Requires -Version 7
<#
.SYNOPSIS
  Runs one `briefing` command as a one-off ECS task in the demo, waits for it, and shows
  its output.

.EXAMPLE
  .\infra\scripts\run-admin.ps1 migrate
  .\infra\scripts\run-admin.ps1 create-user you@example.com "Your Name" --timezone Europe/Dublin
  .\infra\scripts\run-admin.ps1 -Secret create-key you@example.com laptop

.PARAMETER Secret
  The command prints a secret (create-key prints an API key once). Its log stream is
  deleted from CloudWatch after it has been shown here, so the key isn't kept there.
#>
param(
    [switch]$Secret,
    [Parameter(Mandatory, Position = 0, ValueFromRemainingArguments)][string[]]$Command
)

. "$PSScriptRoot\common.ps1"

Use-AwsSession

$cluster = Get-TerraformOutput demo cluster
$taskDef = Get-TerraformOutput demo admin_task_definition
$subnets = Get-TerraformOutput demo task_subnets
$group = Get-TerraformOutput demo admin_security_group
$logGroup = (Get-TerraformOutput demo log_groups).admin

$network = @{ awsvpcConfiguration = @{ subnets = @($subnets); securityGroups = @($group); assignPublicIp = 'ENABLED' } }
$overrides = @{ containerOverrides = @(@{ name = 'admin'; command = @($Command) }) }

# JSON goes through files: quoting JSON on a Windows command line is a trap.
$netFile = New-TemporaryFile
$ovrFile = New-TemporaryFile
try {
    $network | ConvertTo-Json -Depth 5 -Compress | Set-Content $netFile.FullName -NoNewline
    $overrides | ConvertTo-Json -Depth 5 -Compress | Set-Content $ovrFile.FullName -NoNewline
    $taskArn = (Invoke-Native aws ecs run-task --cluster $cluster --task-definition $taskDef `
            --launch-type FARGATE --network-configuration "file://$($netFile.FullName)" `
            --overrides "file://$($ovrFile.FullName)" --query 'tasks[0].taskArn' --output text).Trim()
} finally {
    Remove-Item $netFile.FullName, $ovrFile.FullName -Force
}

$taskId = $taskArn.Split('/')[-1]
Write-Host "Running 'briefing $($Command -join ' ')' as task $taskId ..."
Invoke-Native aws ecs wait tasks-stopped --cluster $cluster --tasks $taskArn

$exit = (Invoke-Native aws ecs describe-tasks --cluster $cluster --tasks $taskArn `
        --query 'tasks[0].containers[0].exitCode' --output text).Trim()
$reason = (Invoke-Native aws ecs describe-tasks --cluster $cluster --tasks $taskArn `
        --query 'tasks[0].stoppedReason' --output text).Trim()

$stream = "admin/admin/$taskId"
Write-Host "--- output ---"
& aws logs get-log-events --log-group-name $logGroup --log-stream-name $stream `
    --start-from-head --query 'events[].message' --output text
Write-Host "--------------"

if ($Secret) {
    & aws logs delete-log-stream --log-group-name $logGroup --log-stream-name $stream
    Write-Host 'Deleted the log stream, so the secret printed above is not kept in CloudWatch.'
}

if ($exit -ne '0') { throw "The task exited with '$exit' ($reason)." }
