"""Checkpoint 1.2: repeatable and recoverable.

- hosts: per-host politeness. A fetch claims the host at dispatch time (Q16's candidate
  fix): `next_allowed_at` moves one interval on, and nothing is reserved while waiting.
- fetch_log: one row per HTTP request, with its start time, so the limiter's spacing is
  tested on what actually happened rather than on slot values.
- sources.consecutive_failures: 401/403/404 five runs in a row marks a source broken.
- deliveries and delivery_attempts: the send-once state machine (§8.3).
- dead_letters: the exact payload and error of every job that died, for replay.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 3

UPGRADE = """
CREATE TABLE hosts (
    host            text PRIMARY KEY,
    min_interval_ms integer NOT NULL DEFAULT 2000 CHECK (min_interval_ms >= 0),
    next_allowed_at timestamptz NOT NULL DEFAULT now(),
    blocked_until   timestamptz
);
-- Horizon's 429 burst came from two Reddit feeds in the same minute (§6.2).
INSERT INTO hosts (host, min_interval_ms) VALUES ('www.reddit.com', 10000);

CREATE TABLE fetch_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   bigint NOT NULL REFERENCES sources (id),
    job_id      bigint,
    host        text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now(),
    status_code integer,
    outcome     text NOT NULL,
    error       text,
    bytes       integer
);
CREATE INDEX fetch_log_host ON fetch_log (host, started_at);
CREATE INDEX fetch_log_source ON fetch_log (source_id, started_at);

ALTER TABLE sources ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0;

CREATE TABLE deliveries (
    id                uuid PRIMARY KEY,
    briefing_id       uuid NOT NULL REFERENCES briefings (id) ON DELETE CASCADE,
    briefing_revision integer NOT NULL,
    channel           text NOT NULL CHECK (channel IN ('email', 'webhook')),
    endpoint_id       uuid,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'in_flight', 'succeeded', 'dead')),
    attempts          integer NOT NULL DEFAULT 0,
    next_attempt_at   timestamptz NOT NULL DEFAULT now(),
    last_error        text,
    message_id        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    succeeded_at      timestamptz,
    CONSTRAINT deliveries_once UNIQUE NULLS NOT DISTINCT
        (briefing_id, briefing_revision, channel, endpoint_id)
);

-- seq numbers every try in the delivery's life; attempt is the delivery's count at the
-- time, which a replay restarts from 1.
CREATE TABLE delivery_attempts (
    delivery_id uuid NOT NULL REFERENCES deliveries (id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    attempt     integer NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    outcome     text CHECK (outcome IN ('succeeded', 'retryable', 'permanent',
                                        'outcome_unknown')),
    error       text,
    PRIMARY KEY (delivery_id, seq)
);

CREATE TABLE dead_letters (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      bigint NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    queue       text NOT NULL,
    payload     jsonb NOT NULL,
    error       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    replayed_at timestamptz
);
CREATE INDEX dead_letters_job ON dead_letters (job_id);
"""

DOWNGRADE = """
DROP TABLE dead_letters, delivery_attempts, deliveries, fetch_log, hosts;
ALTER TABLE sources DROP COLUMN consecutive_failures;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(f"UPDATE schema_meta SET schema_version = {SCHEMA_VERSION}")


def downgrade() -> None:
    op.execute(DOWNGRADE)
    op.execute("UPDATE schema_meta SET schema_version = 2")
