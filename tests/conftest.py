"""Shared fixtures. DB tests build a throwaway `gateway_test` database with the real migrations."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv

from scripts.seed_dev import seed
from shared.db.database import Database

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
    for name, var in (
        ("user", "USER_SERVER_DATABASE_URL"),
        ("knowledge", "KNOWLEDGE_SERVER_DATABASE_URL"),
    ):
        if os.environ.get(var):
            dsns[name] = with_db(os.environ[var], TEST_DB)
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


@pytest.fixture
async def app_db(seeded: dict[str, str]) -> AsyncIterator[Database]:
    """A pool connected as the runtime role (`gateway_app`): what the gateway really uses."""
    db = Database(seeded["app"])
    await db.open()
    yield db
    await db.close()


@pytest.fixture
async def user_db(seeded: dict[str, str]) -> AsyncIterator[Database]:
    """A pool connected as `user_svc`, the user server's least-privilege role."""
    if "user" not in seeded:
        pytest.skip("USER_SERVER_DATABASE_URL not set (see .env.example)")
    db = Database(seeded["user"])
    await db.open()
    yield db
    await db.close()


@pytest.fixture
async def knowledge_db(seeded: dict[str, str]) -> AsyncIterator[Database]:
    """A pool connected as `knowledge_svc`, the knowledge server's read-only role."""
    if "knowledge" not in seeded:
        pytest.skip("KNOWLEDGE_SERVER_DATABASE_URL not set (see .env.example)")
    db = Database(seeded["knowledge"])
    await db.open()
    yield db
    await db.close()
