from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("briefing_engine.db") / "migrations"))
    # Alembic's config uses %-interpolation, so a literal % in a password must be doubled.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
