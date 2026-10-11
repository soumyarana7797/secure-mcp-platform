"""Shared fixtures. DB tests build a throwaway `gateway_test` database with the real migrations."""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv

from scripts.seed_dev import seed

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "gateway_test"
load_dotenv(ROOT / ".env")


def with_db(url: str, name: str) -> str:
    return urlparse(url)._replace(path=f"/{name}").geturl()


@pytest.fixture(scope="session")
def db_dsns() -> dict[str, str]:
    """{'owner': ..., 'app': ...} for a freshly migrated test database, or skip the test."""
    owner, app = (
        os.environ.get("GATEWAY_MIGRATION_DATABASE_URL"),
        os.environ.get("GATEWAY_DATABASE_URL"),
    )
    if not owner or not app:
        pytest.skip("GATEWAY_MIGRATION_DATABASE_URL / GATEWAY_DATABASE_URL not set")
    try:
        with psycopg.connect(
            with_db(owner, "postgres"), autocommit=True, connect_timeout=3
        ) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("PostgreSQL is not running (docker compose up -d)")
    dsns = {"owner": with_db(owner, TEST_DB), "app": with_db(app, TEST_DB)}
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsns["owner"])
    command.upgrade(cfg, "head")
    return dsns


@pytest.fixture
def seeded(db_dsns: dict[str, str]) -> dict[str, str]:
    """Dev tenants/users/grants, with an empty registry and all users active."""
    with psycopg.connect(db_dsns["owner"], autocommit=True) as conn:
        conn.execute("DELETE FROM mcp_tools")
        conn.execute("DELETE FROM mcp_servers")
        conn.execute("UPDATE users SET status = 'active'")
        conn.execute("UPDATE tenants SET status = 'active'")
    asyncio.run(seed(db_dsns["owner"]))
    return db_dsns
