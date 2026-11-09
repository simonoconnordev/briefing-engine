from alembic import context
from sqlalchemy import create_engine, pool

from briefing_engine.config import get_settings
from briefing_engine.db.tables import metadata

config = context.config


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=metadata, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) migrations are not supported.")
run_migrations_online()
