# briefing-engine

A multi-user news-ingestion and LLM-scoring service: it fetches feeds politely, deduplicates articles, scores them against each user's own rubric with Claude, and delivers a daily briefing by email or signed webhook.

**Status: design stage.** No application code yet. The design is in [docs/plan.md](docs/plan.md) and is under review.

Planned stack: Python, FastAPI, PostgreSQL, a Postgres-backed job queue and worker, Docker, GitHub Actions, AWS (ECS Fargate, RDS), Anthropic API.
