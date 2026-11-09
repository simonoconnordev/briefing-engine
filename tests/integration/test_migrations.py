import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from briefing_engine.db.tables import metadata

pytestmark = pytest.mark.integration


def test_schema_version_is_recorded(engine: Engine) -> None:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT schema_version FROM schema_meta")).scalar_one()
    assert version == 3


def test_code_tables_match_migrated_schema(engine: Engine) -> None:
    """The Core definitions in db/tables.py describe exactly what the migrations build."""
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diffs == []
