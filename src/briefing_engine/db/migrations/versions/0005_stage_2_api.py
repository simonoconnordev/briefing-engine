"""Stage 2: the REST API.

- api_keys: per-user keys, stored as SHA-256 of the secret and looked up by a non-secret
  prefix (§5.3, ADR-004).
- idempotency_keys: the stored outcome of a POST that creates work, for 24 hours (§5.1).
- subscriptions_user: the index behind `GET /v1/subscriptions` keyset pagination (§5.4).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 5

UPGRADE = """
CREATE TABLE api_keys (
    id           uuid PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users (id),
    prefix       text NOT NULL UNIQUE CHECK (length(prefix) = 12),
    secret_hash  bytea NOT NULL,
    name         text NOT NULL,
    scopes       text[] NOT NULL CHECK (scopes <@ ARRAY['read', 'write']::text[]),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at   timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_user ON api_keys (user_id);

CREATE TABLE idempotency_keys (
    user_id         uuid NOT NULL REFERENCES users (id),
    key             text NOT NULL,
    request_hash    bytea NOT NULL,
    state           text NOT NULL CHECK (state IN ('in_progress', 'completed')),
    response_status smallint,
    response_body   jsonb,
    locked_until    timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idempotency_keys_expires ON idempotency_keys (expires_at);

CREATE INDEX subscriptions_user ON subscriptions (user_id, created_at, id);
"""

DOWNGRADE = """
DROP INDEX subscriptions_user;
DROP TABLE idempotency_keys, api_keys;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(f"UPDATE schema_meta SET schema_version = {SCHEMA_VERSION}")


def downgrade() -> None:
    op.execute(DOWNGRADE)
    op.execute("UPDATE schema_meta SET schema_version = 4")
