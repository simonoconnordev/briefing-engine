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

**The first briefing comes by itself.** The worker sends each day's briefing once the
user's send time has passed (06:30 by default), and a demo usually starts later than that.
So a build is queued as soon as the profile has its first subscription. It waits up to
10 minutes for that profile's fetches and scores, then sends with whatever is ready, and
that is the day's only briefing. Add all the subscriptions within those 10 minutes. To build
it by hand instead, create the user with `--send-time 23:55`, wait until `GET /v1/articles`
shows scores, then call `POST /v1/briefings`.

`run-admin.ps1 -Secret` deletes the task's CloudWatch log stream after showing it, so an API
key printed by `create-key` isn't kept in the logs.

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
  workflows come with the GitHub repository.

## Known gaps

- DNS rebinding: the feed fetcher checks addresses before connecting but doesn't pin them
  (plan.md, stage 2).
- No CloudWatch alarms yet; logs only.
- RDS rotates its managed password every 7 days. A demo left up longer needs
  `aws ecs update-service --force-new-deployment` after a rotation.
- SES replaces our Message-ID with its own, so a resend after a crash between sending and
  recording can't be de-duplicated by mail clients.
