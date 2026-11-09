#Requires -Version 7
<#
.SYNOPSIS
  Builds the image and pushes it to ECR, tagged with the git commit. Prints the tag.

  Tags are immutable in ECR, so an image that's already there is not pushed again. A build
  from uncommitted changes gets a "-dirty-<time>" suffix, so it never claims a commit's tag.
#>

. "$PSScriptRoot\common.ps1"

Use-AwsSession

$sha = (Invoke-Native git -C $RepoRoot rev-parse --short=12 HEAD).Trim()
$dirty = (& git -C $RepoRoot status --porcelain --untracked-files=no)
$tag = if ($dirty) { "$sha-dirty-$(Get-Date -Format 'yyyyMMddHHmm')" } else { $sha }

$repository = 'briefing-engine'
$registry = "$(Get-AccountId).dkr.ecr.$Region.amazonaws.com"
$image = "$registry/${repository}:$tag"

& aws ecr describe-images --repository-name $repository --image-ids "imageTag=$tag" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "$image is already in ECR."
    return $tag
}

Write-Host "Building $image ..."
Invoke-Native docker build --platform linux/amd64 -t $image $RepoRoot | Out-Host

# The registry password goes straight from the AWS CLI into docker's stdin.
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'docker login to ECR failed' }

Invoke-Native docker push $image | Out-Host
return $tag
