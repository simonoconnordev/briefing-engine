"""Trial feeds and the reader's own verdicts.

- sources.exclude_categories: feed categories whose entries are skipped (Latent Space's
  daily "AINews" roundups, for example), compared case-insensitively.
- profiles.max_age_days: articles published longer ago than this are neither scored nor
  sent, so a newly added feed doesn't flood the first briefing with its back catalogue.
- article_feedback: the reader's "interesting" / "not for me" mark on an article, with an
  optional reason. One row per user and article; marking again replaces the verdict.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 2

UPGRADE = """
ALTER TABLE sources ADD COLUMN exclude_categories text[] NOT NULL DEFAULT '{}';

ALTER TABLE profiles ADD COLUMN max_age_days integer NOT NULL DEFAULT 7
    CHECK (max_age_days BETWEEN 1 AND 365);

CREATE TABLE article_feedback (
    user_id    uuid NOT NULL REFERENCES users (id),
    article_id bigint NOT NULL REFERENCES articles (id),
    verdict    text NOT NULL CHECK (verdict IN ('interesting', 'not_for_me')),
    note       text NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    -- Where the mark was made: a sent briefing, or the blind review of everything scored.
    context    text NOT NULL CHECK (context IN ('briefing', 'review')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, article_id)
);
CREATE INDEX article_feedback_article ON article_feedback (article_id);
"""

DOWNGRADE = """
DROP TABLE article_feedback;
ALTER TABLE profiles DROP COLUMN max_age_days;
ALTER TABLE sources DROP COLUMN exclude_categories;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(f"UPDATE schema_meta SET schema_version = {SCHEMA_VERSION}")


def downgrade() -> None:
    op.execute(DOWNGRADE)
    op.execute("UPDATE schema_meta SET schema_version = 1")
