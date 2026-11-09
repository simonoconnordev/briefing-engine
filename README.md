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

Use `127.0.0.1` rather than `localhost` in URLs. On some Windows machines `localhost` tries IPv6 first and the connection to Postgres hangs.

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

The integration tests need the Postgres container running. They create and use a separate `briefing_test` database, so they never touch your data. No test calls the Anthropic API.
