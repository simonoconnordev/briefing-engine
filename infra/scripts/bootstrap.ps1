#Requires -Version 7
<#
.SYNOPSIS
  First-time setup of infra/base: creates the state bucket and everything else in base,
  then moves base's own state into that bucket. Safe to run again if it stopped partway.

  Before running:
    - aws login --profile briefing-admin
    - the domain is bought at Cloudflare, and CLOUDFLARE_API_TOKEN is in .env
    - infra/base/terraform.tfvars exists (copy terraform.tfvars.example)

  The bucket can't hold the state of the apply that creates it, so the first apply uses
  local state with the S3 backend switched off; then `terraform init -migrate-state` copies
  that state into the bucket, and the local file is deleted once the copy is confirmed.

  Where the state is decides what a run does:
    only in S3               bootstrap has finished; nothing to do
    only local               an earlier run stopped partway: apply again, then move it
    in both                  if they are the same state, the move finished: delete the
                             local copy. Otherwise stop and say so.
    neither, no bucket       a fresh start
    neither, bucket exists   stop: the bucket's state was lost, and only a person can
                             decide what to do about the resources it recorded
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
$localState = Join-Path $dir 'terraform.tfstate'
$stateKey = 'base/terraform.tfstate'
$bucket = Get-StateBucket

# A run killed mid-apply may have left the backend switched off.
if (Test-Path $parked) {
    if (Test-Path $backend) { throw "Both backend.tf and backend.tf.bootstrapping exist in $dir. Keep one." }
    Move-Item $parked $backend
}

function Get-S3Status {
    # 'present' or 'missing'. Anything else (no permission, an expired session, no network)
    # stops the script, so an access problem is never mistaken for a missing bucket.
    param([Parameter(Mandatory)][string[]]$AwsArgs)
    $out = & aws @AwsArgs 2>&1
    if ($LASTEXITCODE -eq 0) { return 'present' }
    $text = ($out | Out-String).Trim()
    if ($text -match '\(404\)|Not Found|NoSuchBucket|NoSuchKey') { return 'missing' }
    throw "aws $($AwsArgs[0]) $($AwsArgs[1]) failed: $text"
}

function Get-RemoteState {
    # Downloads base's remote state to a temp file and returns it parsed. It contains no
    # application secrets (secrets.tf), and nothing here prints it.
    $tmp = New-TemporaryFile
    try {
        Invoke-Native aws s3 cp "s3://$bucket/$stateKey" $tmp.FullName --only-show-errors
        Get-Content $tmp.FullName -Raw | ConvertFrom-Json
    } finally {
        Remove-Item $tmp.FullName -Force
    }
}

function Test-SameState {
    # True when two states record the same resources and outputs. Lineage and serial aren't
    # compared: Terraform 1.17 gives the copy in S3 a new lineage and starts its serial at 1.
    param([Parameter(Mandatory)]$A, [Parameter(Mandatory)]$B)
    $json = { param($s) ConvertTo-Json -InputObject @($s.resources, $s.outputs) -Depth 100 -Compress }
    (& $json $A) -eq (& $json $B)
}

function Remove-LocalState {
    Remove-Item (Join-Path $dir 'terraform.tfstate*') -Force -ErrorAction SilentlyContinue
}

function Read-LocalState {
    # The parsed local state, or $null if there is none worth keeping (no file, or an
    # empty one, which is what a finished migration can leave behind).
    if (-not (Test-Path $localState)) { return $null }
    $raw = Get-Content $localState -Raw
    if (-not $raw -or -not $raw.Trim()) { return $null }
    $state = $raw | ConvertFrom-Json
    if (-not $state.lineage) { return $null }
    $state
}

$hasBucket = (Get-S3Status -AwsArgs @('s3api', 'head-bucket', '--bucket', $bucket)) -eq 'present'
$hasRemote = $hasBucket -and
    (Get-S3Status -AwsArgs @('s3api', 'head-object', '--bucket', $bucket, '--key', $stateKey)) -eq 'present'
$local = Read-LocalState
$hasLocal = $null -ne $local

if ($hasRemote -and -not $hasLocal) {
    Remove-LocalState  # an empty leftover, if any
    Write-Host "Bootstrap has already finished: base's state is in s3://$bucket/$stateKey."
    Write-Host 'Use .\infra\scripts\tf.ps1 base plan (or apply) from here.'
    exit 0
}

if ($hasRemote -and $hasLocal) {
    $remote = Get-RemoteState
    if (Test-SameState $local $remote) {
        Write-Host 'The state had already been moved to S3; removing the local copy.'
        Remove-LocalState
        Write-Host 'Done. Next: .\infra\scripts\put-secrets.ps1'
        exit 0
    }
    throw @"
base's state exists both in s3://$bucket/$stateKey and in $localState, and they differ
(they record different resources or outputs). Bootstrap won't choose between them.
Compare the two, keep the one that matches what's in AWS, and move the other out of the way.
"@
}

if ($hasBucket -and -not $hasLocal) {
    throw @"
The state bucket $bucket exists, but base's state is neither in it nor in $dir.
An earlier run created the bucket and its local state has since been lost, so Terraform no
longer knows about the resources it made. Bootstrap won't guess. Either import them into a
fresh state, or delete what was created (the bucket and anything tagged Root=base) and run
bootstrap again.
"@
}

if ($hasLocal) {
    Write-Host "Resuming: an earlier run left base's state in $localState."
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
# What the copy in S3 must match, read before the move in case the move empties the file.
$local = Read-LocalState
if (-not $local) { throw "The apply finished but left no state in $localState." }

Write-Host "2/3  Moving the state into s3://$bucket/$stateKey ..."
Invoke-Native terraform "-chdir=$dir" init -input=false -migrate-state -force-copy "-backend-config=bucket=$bucket"

Write-Host '3/3  Checking the copy in S3, then removing the local state...'
$remote = Get-RemoteState
if (-not (Test-SameState $local $remote)) {
    throw "The state in S3 doesn't match $localState. The local copy is kept; run bootstrap again."
}
Remove-LocalState

Write-Host 'Done. Next: .\infra\scripts\put-secrets.ps1'
