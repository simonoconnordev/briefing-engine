"""Checkpoint 1.1 tables: one feed, one profile, scoring, the cost ledger and the job queue.

Tables arrive with the checkpoint that needs them (plan §14). Later checkpoints add
deduplication columns, conditional-GET state, hosts, deliveries and budgets by migration.

Revision ID: 0001
Revises: (none)
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_VERSION = 1

UPGRADE = """
CREATE EXTENSION IF NOT EXISTS citext;

-- One row recording the schema version. Readiness compares it with the code's minimum (§4.3).
CREATE TABLE schema_meta (
    one_row        boolean PRIMARY KEY DEFAULT true CHECK (one_row),
    schema_version integer NOT NULL
);

CREATE TABLE users (
    id                    uuid PRIMARY KEY,
    email                 citext NOT NULL UNIQUE,
    display_name          text NOT NULL,
    timezone              text NOT NULL,
    send_time             time NOT NULL,
    monthly_budget_micros bigint NOT NULL DEFAULT 5000000,
    email_enabled         boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    disabled_at           timestamptz
);

CREATE TABLE sources (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('rss', 'atom', 'hn_top')),
    name              text NOT NULL,
    url               text NOT NULL,
    url_normalised    text NOT NULL UNIQUE,
    host              text NOT NULL,
    extract_full_text boolean NOT NULL DEFAULT false,
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'paused', 'broken')),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- url_hash: SHA-256 of the normalised URL, the article's identity.
-- content_hash: SHA-256 of canonicalised title and summary, for syndicated copies (§6.5).
-- input_hash: SHA-256 of exactly what the scorer reads, the score cache key (§7.5).
CREATE TABLE articles (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url_canonical text NOT NULL,
    url_hash      bytea NOT NULL UNIQUE,
    content_hash  bytea NOT NULL,
    input_hash    bytea NOT NULL,
    title         text NOT NULL,
    author        text,
    published_at  timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    summary_text  text NOT NULL DEFAULT ''
);
CREATE INDEX articles_content_hash ON articles (content_hash);
CREATE INDEX articles_first_seen ON articles (first_seen_at, id);

CREATE TABLE article_sources (
    article_id    bigint NOT NULL REFERENCES articles (id),
    source_id     bigint NOT NULL REFERENCES sources (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, source_id)
);

CREATE TABLE profiles (
    id              uuid PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users (id),
    name            text NOT NULL,
    current_version integer NOT NULL,
    threshold       real NOT NULL,
    max_items       integer NOT NULL DEFAULT 12,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, name),
    UNIQUE (id, user_id)  -- target of the subscriptions composite foreign key
);

-- Rows are never changed: editing a rubric inserts a new version.
CREATE TABLE profile_versions (
    profile_id  uuid NOT NULL REFERENCES profiles (id),
    version     integer NOT NULL,
    rubric      jsonb NOT NULL,
    rubric_hash bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, version)
);

-- The composite foreign key means a subscription can only point at its owner's profile.
CREATE TABLE subscriptions (
    id         uuid PRIMARY KEY,
    user_id    uuid NOT NULL,
    source_id  bigint NOT NULL REFERENCES sources (id),
    profile_id uuid NOT NULL,
    weight     real NOT NULL DEFAULT 1.0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_id, source_id),
    FOREIGN KEY (profile_id, user_id) REFERENCES profiles (id, user_id)
);
CREATE INDEX subscriptions_source ON subscriptions (source_id);

-- The cost ledger: one row per API call, charged to a user (§7.7).
CREATE TABLE llm_calls (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            uuid NOT NULL REFERENCES users (id),
    purpose            text NOT NULL
                       CHECK (purpose IN ('score', 'writeup', 'enrich', 'eval', 'smoke')),
    model              text NOT NULL,
    request_id         text,
    input_tokens       integer NOT NULL DEFAULT 0,
    output_tokens      integer NOT NULL DEFAULT 0,
    cache_read_tokens  integer NOT NULL DEFAULT 0,
    cache_write_tokens integer NOT NULL DEFAULT 0,
    cost_micros        bigint NOT NULL,
    estimate_micros    bigint NOT NULL,
    latency_ms         integer NOT NULL,
    outcome            text NOT NULL
                       CHECK (outcome IN ('ok', 'invalid_output', 'refusal', 'max_tokens',
                                          'api_error')),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_user_created ON llm_calls (user_id, created_at);

-- The score cache. Only valid scores are stored; failures are never cached (§7.5).
CREATE TABLE llm_scores (
    input_hash     bytea NOT NULL,
    rubric_hash    bytea NOT NULL,
    model          text NOT NULL,
    prompt_version integer NOT NULL,
    score          smallint NOT NULL CHECK (score BETWEEN 0 AND 10),
    reason         text NOT NULL,
    topics         text[] NOT NULL DEFAULT '{}',
    method         text NOT NULL CHECK (method IN ('llm', 'llm_retry')),
    llm_call_id    bigint NOT NULL REFERENCES llm_calls (id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (input_hash, rubric_hash, model, prompt_version)
);

-- UNIQUE(user_id, profile_id, briefing_date) is what makes re-running a day idempotent.
CREATE TABLE briefings (
    id              uuid PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users (id),
    profile_id      uuid NOT NULL,
    profile_version integer NOT NULL,
    briefing_date   date NOT NULL,
    revision        integer NOT NULL DEFAULT 1,
    status          text NOT NULL CHECK (status IN ('building', 'ready', 'degraded', 'failed')),
    writeup         jsonb,
    cost_micros     bigint NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    ready_at        timestamptz,
    UNIQUE (user_id, profile_id, briefing_date),
    FOREIGN KEY (profile_id, profile_version) REFERENCES profile_versions (profile_id, version)
);
CREATE INDEX briefings_user_date ON briefings (user_id, briefing_date, id);

CREATE TABLE briefing_items (
    briefing_id uuid NOT NULL REFERENCES briefings (id) ON DELETE CASCADE,
    article_id  bigint NOT NULL REFERENCES articles (id),
    rank        smallint NOT NULL,
    score       smallint NOT NULL,
    blurb       text NOT NULL,
    group_key   text,
    PRIMARY KEY (briefing_id, article_id),
    UNIQUE (briefing_id, rank)
);

-- The job queue (§9.1). There is no 'failed' state: a failure that will be retried goes
-- back to 'queued', and one that won't becomes 'dead'.
CREATE TABLE jobs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    queue            text NOT NULL
                     CHECK (queue IN ('fetch', 'extract', 'score', 'build', 'deliver',
                                      'maintenance')),
    kind             text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}',
    dedupe_key       text,
    group_key        text,
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'succeeded', 'dead')),
    priority         smallint NOT NULL DEFAULT 0,
    run_at           timestamptz NOT NULL DEFAULT now(),
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 5,
    deferrals        integer NOT NULL DEFAULT 0,
    locked_by        text,
    lease_expires_at timestamptz,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
-- At most one live job per dedupe key; finished jobs don't block a new one.
CREATE UNIQUE INDEX jobs_dedupe_live ON jobs (dedupe_key) WHERE state IN ('queued', 'running');
CREATE INDEX jobs_claim ON jobs (queue, priority DESC, run_at) WHERE state = 'queued';
CREATE INDEX jobs_group ON jobs (queue, group_key, run_at) WHERE state = 'queued';
CREATE INDEX jobs_lease ON jobs (lease_expires_at) WHERE state = 'running';
"""

DOWNGRADE = """
DROP TABLE jobs, briefing_items, briefings, llm_scores, llm_calls, subscriptions,
           profile_versions, profiles, article_sources, articles, sources, users, schema_meta;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(f"INSERT INTO schema_meta (schema_version) VALUES ({SCHEMA_VERSION})")


def downgrade() -> None:
    op.execute(DOWNGRADE)
