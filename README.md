# briefing-engine

A multi-user news-ingestion and LLM-scoring service: it fetches feeds politely, deduplicates articles, scores them against each user's own rubric with Claude, and delivers a daily briefing by email or signed webhook.

**Status: checkpoint 1.1 (one complete briefing).** One feed, one profile, a Postgres job queue with one worker, Claude Haiku scoring with structured output, a cost ledger, and an email of the top-scored articles. It runs by hand; there is no scheduler yet. The design and the remaining checkpoints are in [docs/plan.md](docs/plan.md).

Planned stack: Python, FastAPI, PostgreSQL, a Postgres-backed job queue and worker, Docker, GitHub Actions, AWS (ECS Fargate, RDS), Anthropic API.

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
   uv run briefing run-daily -v
   ```

6. Read the email in Mailpit at <http://127.0.0.1:8025>, and check what it cost:

   ```bash
   uv run briefing usage
   ```

Running `run-daily` again for the same date makes no LLM calls and sends no email: scores come from the cache and the briefing already exists. `uv run briefing jobs` shows the queue.

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
