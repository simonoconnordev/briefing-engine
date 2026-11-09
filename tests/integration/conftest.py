"""Integration tests run against a real Postgres 17: the one from compose.yaml locally,
a service container in CI.

The session fixture recreates a dedicated `briefing_test` database and migrates it, so
tests never touch the development database. Each test starts from empty tables.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, make_url, text

from briefing_engine.db.engine import upgrade_to_head

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://briefing:briefing@127.0.0.1:5432/briefing_test",
)

KEEP_TABLES = {"schema_meta", "alembic_version"}


def _recreate_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    try:
        _recreate_database(TEST_DATABASE_URL)
    except Exception as exc:  # pragma: no cover - depends on the developer's machine
        pytest.skip(f"Postgres is not reachable ({exc.__class__.__name__}); run docker compose up")
    upgrade_to_head(TEST_DATABASE_URL)
    eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Engine:
    """The migrated test database with every data table emptied."""
    with engine.begin() as conn:
        names = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        tables = [n for n in names if n not in KEEP_TABLES]
        if tables:
            joined = ", ".join(f'"{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))
    return engine
