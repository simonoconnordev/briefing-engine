# Running Briefing Engine on AWS

Stage 2's AWS half, in **demo mode**: the expensive parts exist only while a demo is up.

| Root | What | Lifetime | Cost |
|---|---|---|---|
| `infra/base/` | State bucket, ECR, SES (domain, DKIM, MAIL FROM, DMARC), the API's certificate, SSM parameters, GitHub OIDC roles | Applied once, kept | Cents a month |
| `infra/demo/` | VPC, RDS Postgres, load balancer, ECS cluster with the `api` and `worker` services, logs, the `api.<domain>` record | `demo-up.ps1` … `demo-down.ps1` | ~$65/month if left up; ~$0.10 an hour |

DNS stays at Cloudflare, where the domain is registered; Terraform writes the records there.
Region: `eu-west-1`. Credentials come from `aws login` (no access keys on disk).

## You need

- PowerShell 7, the AWS CLI 2.32 or later, Terraform 1.10 or later, and Docker Desktop running.
- `aws login --profile briefing-admin` done in the last 12 hours.
- The domain at Cloudflare, and a Cloudflare API token with **Zone → Zone → Read** and
  **Zone → DNS → Edit**, both limited to that one zone, in `.env` as `CLOUDFLARE_API_TOKEN`.
  Terraform needs the read permission to look the zone up by name.
- `ANTHROPIC_API_KEY` in `.env`.

## Once: the base

```powershell
Copy-Item infra\base\terraform.tfvars.example infra\base\terraform.tfvars   # then edit it
.\infra\scripts\bootstrap.ps1      # applies base with local state, then moves it into S3
.\infra\scripts\put-secrets.ps1    # creates the Anthropic key and a cursor key in SSM
```

If bootstrap stops partway (a failed apply, a lost session), fix the cause and run it again:
it works out from where the state is (local, S3, or both) whether to resume, tidy up or stop
and explain. It keeps the local state until the copy in S3 is confirmed.

AWS emails each address in `ses_verified_recipients`; click the link in it. SES is in its
sandbox, so it delivers only to verified addresses (up to 200 a day).

Later changes to base: `.\infra\scripts\tf.ps1 base plan`, then `... base apply`.

## Each demo

```powershell
.\infra\scripts\demo-up.ps1        # push the image, apply demo, migrate, restart services
.\infra\scripts\run-admin.ps1 create-user you@example.com "Your Name" --timezone Europe/Dublin
.\infra\scripts\run-admin.ps1 -Secret create-key you@example.com laptop   # read,write by default
# ... use https://api.<domain>/docs with the key ...
.\infra\scripts\demo-down.ps1      # destroy everything in demo/
```

The demo database is fresh each time (no final snapshot): profiles and subscriptions are set
up through the API. My real reading history stays in the local Postgres.

**The first briefing comes by itself.** The worker builds each day's briefing from five
minutes before the user's send time (06:30 by default), and a demo usually starts later than
that. So the scheduler queues a build on its next check after the profile gets its first
subscription, usually within a minute. The build waits while that profile's fetches and
scores are due, for 10 minutes at most, and goes ahead as soon as they are done. It sends
with whatever is ready, and that is the day's only briefing. A feed subscribed while that
work is still running is waited for too; one added after it finishes misses the day's
briefing. The 10 minutes are a limit, not time set aside for setup. To set everything up
first and build by hand, create the user with `--send-time 23:55` (the automatic build then
starts at 23:50), wait until `GET /v1/articles` shows scores, then call `POST /v1/briefings`.

`run-admin.ps1 -Secret` deletes the task's CloudWatch log stream after showing it, so an API
key printed by `create-key` isn't kept in the logs.

## GitHub Actions

Two workflows use the CI roles in base. Neither needs AWS keys: each job asks GitHub for an
OIDC token and exchanges it for a role that trusts only that kind of job.

- **Deploy** (the `deploy` job in `.github/workflows/ci.yml`). After the tests pass on
  `main`, it waits for approval in the `production` environment, then pushes the image for
  that commit to ECR. If the demo is up, it also migrates the database and rolls the image
  out to both services, as `demo-up.ps1` does, and checks `/readyz`. If the demo is down it
  stops after the push, and the next `demo-up.ps1` finds the image already there. It can't
  run Terraform, so a rollout registers new task-definition revisions that change only the
  image (`infra/ci/deploy.sh`). A deployment left unapproved does nothing.
- **Terraform plan** (`.github/workflows/terraform-plan.yml`). On a pull request that
  changes `infra/base` or `infra/demo`, it plans both roots with the read-only plan role.
  The logs of a public repository are public, so Terraform's output, which includes the
  account ID and the SES recipients, stays out of them. The job summary lists only the
  resources that would change. While the demo is down, the demo plan shows its 46 resources
  to add.

**Setting them up, once.** Run these from the repository root after `aws login`. Repository
settings are the owner's to change, so these are done by hand.

1. Create the `production` environment. In the repository's **Settings → Environments**, add
   `production`, tick **Required reviewers** and add yourself, and under **Deployment
   branches and tags** choose **Selected branches and tags** and add `main`.
2. Give the workflows the role ARNs and the values from `terraform.tfvars`. The ARNs contain
   the account ID, so they go in as secrets, which GitHub masks in logs:

   ```powershell
   .\infra\scripts\tf.ps1 base output -raw github_deploy_role_arn | gh secret set AWS_DEPLOY_ROLE_ARN --env production
   .\infra\scripts\tf.ps1 base output -raw github_plan_role_arn | gh secret set AWS_PLAN_ROLE_ARN
   gh secret set TF_VAR_SES_VERIFIED_RECIPIENTS   # paste the list as in terraform.tfvars: ["you@example.com"]
   gh variable set TF_VAR_DOMAIN --body example.com
   gh variable set API_BASE_URL --body https://api.example.com
   ```

3. Create a second Cloudflare API token for the plans, with **Zone → Zone → Read** and
   **Zone → DNS → Read** on the one zone, and store it with
   `gh secret set CLOUDFLARE_API_TOKEN_READ`. The token in `.env` can edit DNS, and CI only
   ever needs to read it.

Both roles trust this one repository only, by the subject GitHub puts in its OIDC tokens
(`github_oidc_subject` in base). New repositories use GitHub's immutable form, which names the
owner and repository by their permanent IDs as well as their names:
`repo:simonoconnordev@195565840/briefing-engine@1460610921`. A fork, or the code pushed to a
new repository, needs that variable set to its own subject and base applied again. Read it with
`gh api repos/<owner>/<name>/actions/oidc/customization/sub --jq .sub_claim_prefix`.

## How the pieces fit

- **One image** (`Dockerfile`) runs as `api`, `worker` and one-off `admin` tasks (migrate,
  create-user, create-key). Images are tagged with the git commit; tags are immutable.
- **Secrets.** The Anthropic key and the cursor key are SSM SecureStrings. `put-secrets.ps1`
  creates them, and Terraform knows only their names and ARNs, so the values are never in
  Terraform state. (A Terraform-managed parameter with `ignore_changes` would not do: every
  refresh reads the decrypted value back into state.) The CI plan role is denied reading
  them. RDS keeps the database password in Secrets Manager itself. ECS injects all three into
  the containers; the app builds its database URL from `DB_HOST`, `DB_USER`, `DB_PASSWORD` and
  so on, with `sslmode=require`.
- **Email** goes through the SES API with the worker's task role. No SMTP password exists.
- **Network.** Tasks run in public subnets with public IPs (no NAT gateway, the biggest
  saving), and their security groups admit nothing except port 8080 from the load balancer.
  The database is in private subnets with no route out, reachable only from the tasks.
- **Health.** The load balancer checks `/healthz`; `/readyz` (database and schema) is for the
  deploy step and for people.
- **CI roles** exist in base (`briefing-engine-github-deploy`, `-plan`), trusted only for the
  `production` environment and pull requests of `simonoconnordev/briefing-engine`. The
  workflows that use them are described under [GitHub Actions](#github-actions).

## Known gaps

- DNS rebinding: the feed fetcher checks addresses before connecting but doesn't pin them
  (plan.md, stage 2).
- No CloudWatch alarms yet; logs only.
- RDS rotates its managed password every 7 days. A demo left up longer needs
  `aws ecs update-service --force-new-deployment` after a rotation.
- SES replaces our Message-ID with its own, so a resend after a crash between sending and
  recording can't be de-duplicated by mail clients.
