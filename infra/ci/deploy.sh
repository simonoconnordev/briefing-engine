#!/usr/bin/env bash
# Run by the deploy job in .github/workflows/ci.yml, with the deploy role's credentials.
#
# Pushes the image for this commit to ECR. If the demo is up, it then rolls the image out the
# way demo-up.ps1 does: `briefing migrate` as a one-off task first, then the api and worker
# services, then /readyz as the final check. If the demo is down it stops after the push, and
# the next demo-up.ps1 finds the image already in ECR.
#
# The deploy role can push images and update ECS, but not run Terraform (infra/base/github.tf).
# So a rollout registers new task-definition revisions that differ only in the image. The next
# demo-up.ps1 apply replaces them, and demo-down.ps1 leaves them behind as inactive history.
#
# Nothing here prints the account ID: the logs of a public repository are public.

set -euo pipefail

: "${AWS_REGION:?}" "${GITHUB_SHA:?}"
cluster=briefing-engine
repository=briefing-engine
tag=${GITHUB_SHA:0:12} # the same tag push-image.ps1 gives a clean checkout
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

note() { echo "$1"; echo "$1" >>"$summary"; }
fail() { echo "::error::$1"; echo "**Failed:** $1" >>"$summary"; exit 1; }

account=$(aws sts get-caller-identity --query Account --output text)
echo "::add-mask::$account"
registry=$account.dkr.ecr.$AWS_REGION.amazonaws.com
image=$registry/$repository:$tag

# Tags are immutable in ECR, so an image that's already there is kept as it is.
if aws ecr describe-images --repository-name "$repository" --image-ids "imageTag=$tag" >/dev/null 2>&1; then
    note "The image \`$tag\` was already in ECR."
else
    docker build --platform linux/amd64 -t "$image" .
    aws ecr get-login-password | docker login --username AWS --password-stdin "$registry"
    docker push "$image"
    docker logout "$registry"
    note "Pushed the image \`$tag\` to ECR."
fi

# The demo is up only while infra/demo exists. A missing cluster means it's down; any other
# error is a real failure and stops the job.
if ! aws ecs describe-services --cluster "$cluster" --services api worker --output json \
    >"$work/services.json" 2>"$work/error.txt"; then
    if grep -q ClusterNotFoundException "$work/error.txt"; then
        note "The demo is down, so there is nothing to roll out."
        exit 0
    fi
    cat "$work/error.txt" >&2
    fail "Couldn't read the demo's ECS services."
fi
active=$(jq '[.services[] | select(.status == "ACTIVE")] | length' "$work/services.json")
if [[ $active != 2 ]]; then
    note "The demo is down, so there is nothing to roll out."
    exit 0
fi

# Registers a copy of a task definition with only the image changed, and prints its ARN.
# Fields that describe-task-definition returns but register-task-definition rejects are dropped.
new_revision() {
    aws ecs describe-task-definition --task-definition "$1" --query taskDefinition --output json |
        jq --arg image "$image" '
            .containerDefinitions[0].image = $image
            | {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions, volumes,
               placementConstraints, requiresCompatibilities, cpu, memory, runtimePlatform}
            | with_entries(select(.value != null))' >"$work/task-definition.json"
    aws ecs register-task-definition --cli-input-json "file://$work/task-definition.json" \
        --query taskDefinition.taskDefinitionArn --output text
}

service_field() { jq -r --arg s "$1" ".services[] | select(.serviceName == \$s) | $2" "$work/services.json"; }

# Plain assignments, so that a failed registration stops the script (set -e).
admin=$(new_revision briefing-engine-admin)
api=$(new_revision "$(service_field api .taskDefinition)")
worker=$(new_revision "$(service_field worker .taskDefinition)")
declare -A next=([api]=$api [worker]=$worker)

# Migrations run before the new code starts, and are written to work with the old code too,
# so the tasks still running stay healthy (plan.md, §4.3). The admin task uses the worker's
# subnets and security group, as run-admin.ps1 does.
service_field worker .networkConfiguration >"$work/network.json"
task=$(aws ecs run-task --cluster "$cluster" --task-definition "$admin" --launch-type FARGATE \
    --network-configuration "file://$work/network.json" --query 'tasks[0].taskArn' --output text)
[[ $task == arn:* ]] || fail "ECS didn't start the migration task."
echo "Running briefing migrate as task ${task##*/} ..."
aws ecs wait tasks-stopped --cluster "$cluster" --tasks "$task"
exit_code=$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" \
    --query 'tasks[0].containers[0].exitCode' --output text)
[[ $exit_code == 0 ]] ||
    fail "The migration exited with '$exit_code'. Its output is in CloudWatch, /briefing-engine/admin. Nothing was rolled out."
note "Migrated the database."

for service in api worker; do
    aws ecs update-service --cluster "$cluster" --service "$service" \
        --task-definition "${next[$service]}" --query service.serviceName --output text >/dev/null
done
echo "Waiting for the services to settle ..."
aws ecs wait services-stable --cluster "$cluster" --services api worker

# A failed rollout is undone by the circuit breaker, and a rolled-back service is stable too,
# so check that the new revision is the one running.
for service in api worker; do
    read -r running state < <(aws ecs describe-services --cluster "$cluster" --services "$service" \
        --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition, rolloutState]' \
        --output text)
    [[ $running == "${next[$service]}" && $state == COMPLETED ]] ||
        fail "The $service service didn't take the new image (rollout state: $state). ECS rolled it back to the previous one."
done
note "Rolled out \`$tag\` to the api and worker services."

# Stable services mean the load balancer's liveness check passes. /readyz also checks the
# database and the schema.
: "${API_BASE_URL:?Set the API_BASE_URL repository variable, e.g. https://api.example.com}"
code=000
for _ in $(seq 36); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API_BASE_URL/readyz" || true)
    [[ $code == 200 ]] && break
    sleep 5
done
[[ $code == 200 ]] || fail "$API_BASE_URL/readyz answered $code after 3 minutes. Check the api service's logs."
note "$API_BASE_URL/readyz is ready."
