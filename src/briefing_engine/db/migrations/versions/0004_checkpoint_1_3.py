"""Checkpoint 1.3: conditional GET and the briefing write-up.

- sources.etag, last_modified: the validators from the last 200, sent back as
  If-None-Match and If-Modified-Since (§6.4).
- sources.last_body_sha256: a feed that ignores validators but sends the same bytes again
  is skipped without parsing (the "soft 304").
- llm_writeups: the write-up cache (§7.6), keyed by the items it was written for, the
  rubric, the model and the prompt version.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 4

UPGRADE = """
ALTER TABLE sources
    ADD COLUMN etag             text,
    ADD COLUMN last_modified    text,
    ADD COLUMN last_body_sha256 bytea;

CREATE TABLE llm_writeups (
    items_hash     bytea NOT NULL,
    rubric_hash    bytea NOT NULL,
    model          text NOT NULL,
    prompt_version integer NOT NULL,
    writeup        jsonb NOT NULL,
    llm_call_id    bigint NOT NULL REFERENCES llm_calls (id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (items_hash, rubric_hash, model, prompt_version)
);
"""

DOWNGRADE = """
DROP TABLE llm_writeups;
ALTER TABLE sources DROP COLUMN etag, DROP COLUMN last_modified, DROP COLUMN last_body_sha256;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(f"UPDATE schema_meta SET schema_version = {SCHEMA_VERSION}")


def downgrade() -> None:
    op.execute(DOWNGRADE)
    op.execute("UPDATE schema_meta SET schema_version = 3")
