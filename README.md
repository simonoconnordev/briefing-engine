# briefing-engine

A multi-user news-ingestion and LLM-scoring service: it fetches feeds politely, deduplicates articles, scores them against each user's own rubric with Claude, and delivers a daily briefing by email (signed webhooks are planned for stage 3).

**Status: checkpoint 1.3 built, and stage 2 built locally (the REST API, per-user schedules, one container image). The AWS half (Terraform for demo mode, SES email) ran end to end on 16 November 2026: a new user signed up through the API and received a scored and written briefing by email, and the demo was then taken down. See [infra/README.md](infra/README.md).** Five feeds and one profile; a Postgres job queue with retries, leases and dead letters; polite fetching (per-host spacing, `Retry-After`, conditional GET); Claude Haiku scores each article against the rubric, and Claude Sonnet writes the briefing's headline, introduction and a short paragraph per item; a cost ledger; one email per day however often a run is repeated (see "When something fails" for the one crash that can send it twice); and a local page for marking what was worth reading. A script runs it daily from Windows Task Scheduler. The design and the remaining checkpoints are in [docs/plan.md](docs/plan.md).

Planned stack: Python, FastAPI, PostgreSQL, a Postgres-backed job queue and worker, Docker, GitHub Actions, AWS (ECS Fargate, RDS), Anthropic API.

## Read the write-up

I wrote up the project as a four-part guide on my website, Endpoint 51. Each part follows on from the one before:

1. [How I built Briefing Engine](https://www.endpoint51.com/briefing-engine/): why I built a second daily AI news briefing when Horizon already sent me one, and how the project took shape.
2. [Inside Briefing Engine](https://www.endpoint51.com/briefing-engine/inside/): one article followed from a publisher's feed through scoring and selection to a reader's inbox, and why each step is built the way it is.
3. [Running Briefing Engine locally](https://www.endpoint51.com/briefing-engine/running-locally/): installing it on Windows, starting Postgres and Mailpit, and reading the first briefing in Mailpit.
4. [Briefing Engine's first AWS deployment](https://www.endpoint51.com/briefing-engine/first-aws-deployment/): what the first deployment to AWS revealed, up to the first briefing reaching Gmail.

## Run it locally

**Prerequisites:** Docker Desktop (on Windows, with WSL 2) and [uv](https://docs.astral.sh/uv/). uv installs Python 3.14 for the project, so your system Python doesn't matter.

1. Start Postgres 17 and Mailpit (both bound to 127.0.0.1 only):

   ```bash
   docker compose up -d
   ```

2. Install the project and its dependencies:

   ```bash
   uv sync
   ```

3. Copy the example settings, then put your Anthropic API key in `.env`. Both copies are gitignored.

   ```bash
   cp .env.example .env
   cp briefing.example.toml briefing.toml
   ```

   Edit `briefing.toml` to change the recipient, the profile's rubric or the feed.

4. Create the tables and load the config:

   ```bash
   uv run briefing migrate
   uv run briefing load-config
   ```

5. Run a day:

   ```bash
   uv run briefing -v run-daily
   ```

6. Read the email in Mailpit at <http://127.0.0.1:8025>, and check what it cost:

   ```bash
   uv run briefing usage
   ```

Running `run-daily` again for the same date makes no LLM calls and sends no email: scores come from the cache and the briefing already exists. Feeds that haven't changed answer `304 Not Modified`, so a re-run downloads almost nothing. `uv run briefing jobs` shows the queue.

A typical day costs about 2–3 cents: well under a cent for Haiku's scoring, and about 2 cents for Sonnet's write-up. To send the scorer's one-line reasons instead of a write-up, set `WRITEUP_MODEL=` (empty) in `.env`. If the write-up call fails, the briefing still goes out with those reasons and says so.

### Run it every day (Windows)

`scripts/run-daily.ps1` starts the containers if needed, runs `run-daily` and appends its output to `logs/run-daily-<date>.log`. To have Task Scheduler run it at 06:00 (or `-At 05:30`, say):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-daily.ps1
```

The task runs as you while you're signed in, and if the PC was asleep at 06:00 it runs when it wakes. Docker Desktop must be set to start at sign-in. Remove the task with `-Remove`.

### When something fails

- **A feed or the model API has a temporary problem** (a timeout, a 5xx, a rate limit): the job is retried with backoff. `run-daily` waits for retries due within two minutes; later ones run on the next `run-daily`.
- **A site asks us to slow down** (429 or 503 with `Retry-After`): every feed on that host waits until then. Requests to one host are spaced at least 2 s apart (10 s for Reddit), however many workers run.
- **A feed keeps failing** (401, 403 or 404 five runs in a row, or one 410): it's marked `broken` and no longer fetched.
- **An email fails to send:** it's retried after 1 min, 5 min, 30 min, 2 h, 6 h and 12 h. An SMTP 5xx (a rejected address, say) stops it at once.
- **A job runs out of attempts:** it's `dead`, and `uv run briefing jobs` lists it with its error. After fixing the cause, run it again from its first attempt:

  ```bash
  uv run briefing jobs replay 123
  ```

If the worker is killed partway through, the next run picks its jobs up once their lease expires (60 s). An email is marked as being sent before it goes and as sent after, so the only crash that can send it twice is one between the two; the copy carries the same Message-ID.

## The API and the worker (stage 2, local)

For more than one user, the daily script gives way to two long-running processes, both from one Docker image:

- `briefing api`: the REST API on <http://127.0.0.1:8080>. Interactive docs are at [/docs](http://127.0.0.1:8080/docs).
- `briefing worker`: fetches feeds hourly (every 15 minutes just before someone's send time), builds each user's briefing at their own local send time, and sends it.

Run both in containers, beside Postgres and Mailpit:

```bash
docker compose --profile app up -d --build
```

A one-shot `migrate` container runs the migrations first. Run either the worker or the daily scheduled task, not both. Running both is safe (each briefing is built and sent once), but it's confusing.

Users and keys are admin commands, not endpoints:

```bash
uv run briefing create-user friend@example.com "A friend" --timezone Europe/London --send-time 07:00
uv run briefing create-key friend@example.com laptop          # prints the key once
uv run briefing set-budget friend@example.com 5
```

The new user then does everything else through the API:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/profiles -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "ai-news", "threshold": 6, "rubric": {"description": "Practical AI engineering", "interests": [{"topic": "agents", "weight": 5}], "scale_hint": "10 means read it today"}}'
curl -s -X POST http://127.0.0.1:8080/v1/subscriptions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"url": "https://simonwillison.net/atom/everything/", "profile": "ai-news"}'
curl -s http://127.0.0.1:8080/v1/briefings -H "Authorization: Bearer $KEY"
```

A feed already in the system is shared, so it's fetched once for everyone who reads it. Each user sees only their own profiles, scores, briefings, jobs and costs. A URL that resolves to a private or internal address is refused (422), and so is a redirect to one at fetch time.

Set `CURSOR_HMAC_KEY` in `.env` to any long random string; it signs the pagination cursors. Without it, cursors stop working whenever the API restarts.

## Teach it what you find interesting

The briefing improves from your verdicts, not from guesses about your taste.

```bash
uv run briefing web
```

This opens a local page at <http://127.0.0.1:8000> (the email links to it):

- **Latest briefing:** mark each item *Interesting* or *Not for me*, with a few words on why.
- **Review:** everything scored in the last few days, including what wasn't sent. Claude's score stays hidden until you've marked an item, so it can't sway you.
- **Feeds report:** what each feed contributes (fetched, scored, above the threshold, sent, and your marks), and the items where Claude and you disagree.

`uv run briefing report` prints the same report in the terminal. The disagreements, and your notes on them, are what to edit the rubric in `briefing.toml` from.

Use `127.0.0.1` rather than `localhost` in URLs. On some Windows machines `localhost` tries IPv6 first and the connection to Postgres hangs.

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

The integration tests need the Postgres container running. They create and use a separate `briefing_test` database, so they never touch your data. No test calls the Anthropic API.
