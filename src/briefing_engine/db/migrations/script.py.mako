"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

# Set to this migration's sequence number; readiness compares it with MIN_SCHEMA_VERSION.
SCHEMA_VERSION = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}
    op.execute(f"UPDATE schema_meta SET schema_version = {SCHEMA_VERSION}")


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
