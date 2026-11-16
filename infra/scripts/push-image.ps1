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

# Docker Desktop keeps logins in Windows Credential Manager, which rejects ECR's long token
# ("The stub received bad data"). So the login and push use a throwaway Docker config that
# holds the token (valid 12 hours) only until the push is done. Docker falls back to Credential
# Manager whenever it finds docker-credential-wincred on PATH, and that sits next to docker.exe,
# so for these two commands docker is run by its full path with its folder taken off PATH.
# The engine and the image built above are unchanged; DOCKER_HOST keeps the current engine.
$docker = (Get-Command docker -CommandType Application | Select-Object -First 1).Source
$dockerBin = Split-Path $docker
$dockerHost = (Invoke-Native $docker context inspect --format '{{.Endpoints.docker.Host}}' | Out-String).Trim()
$dockerConfig = Join-Path ([IO.Path]::GetTempPath()) "briefing-engine-docker-$PID"
New-Item -ItemType Directory -Force $dockerConfig | Out-Null
Set-Content -Path (Join-Path $dockerConfig 'config.json') -Value '{}'
$saved = @{ DOCKER_CONFIG = $env:DOCKER_CONFIG; DOCKER_HOST = $env:DOCKER_HOST; PATH = $env:PATH }
try {
    $env:DOCKER_CONFIG = $dockerConfig
    $env:DOCKER_HOST = $dockerHost
    $env:PATH = ($env:PATH -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $dockerBin }) -join ';'
    # The registry password goes straight from the AWS CLI into docker's stdin.
    aws ecr get-login-password --region $Region | & $docker login --username AWS --password-stdin $registry | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'docker login to ECR failed' }
    Invoke-Native $docker push $image | Out-Host
} finally {
    foreach ($name in $saved.Keys) {
        if ($null -eq $saved[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $saved[$name] }
    }
    Remove-Item $dockerConfig -Recurse -Force -ErrorAction SilentlyContinue
}
return $tag
