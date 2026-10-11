"""Alembic environment. Migrations are raw SQL (no ORM models), run as the owner role."""

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()
config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ["GATEWAY_MIGRATION_DATABASE_URL"]
    # SQLAlchemy needs the driver named explicitly; we use psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
