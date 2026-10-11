"""Seeds DEV tenants, users and role permissions (owner DB role). Idempotent. Dev data only."""

import asyncio
import os

from dotenv import load_dotenv
from psycopg import AsyncConnection

load_dotenv()

TENANTS = [("acme", "Acme Corp"), ("globex", "Globex")]
# (tenant, cognito_sub, display name, roles)
USERS = [
    ("acme", "sub-alice", "Alice", ["analyst"]),
    ("acme", "sub-bob", "Bob", ["viewer"]),
    ("globex", "sub-carol", "Carol", ["analyst"]),
]
# (tenant, role, permission)
ROLE_PERMISSIONS = [
    ("acme", "analyst", "football:read"),
    ("acme", "analyst", "users:read"),
    ("acme", "analyst", "knowledge:read"),
    ("acme", "viewer", "football:read"),
    ("globex", "analyst", "football:read"),
]


async def seed(dsn: str) -> None:
    async with await AsyncConnection.connect(dsn) as conn:
        async with conn.transaction():
            for slug, name in TENANTS:
                await conn.execute(
                    "INSERT INTO tenants (slug, name) VALUES (%s, %s) "
                    "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name",
                    (slug, name),
                )
            for tenant, sub, display, roles in USERS:
                await conn.execute(
                    "INSERT INTO users (tenant_id, cognito_sub, display_name, roles) "
                    "SELECT id, %s, %s, %s FROM tenants WHERE slug = %s "
                    "ON CONFLICT (cognito_sub) DO UPDATE SET display_name = EXCLUDED.display_name, "
                    "roles = EXCLUDED.roles",
                    (sub, display, roles, tenant),
                )
            await conn.execute("DELETE FROM role_permissions")
            for tenant, role, permission in ROLE_PERMISSIONS:
                await conn.execute(
                    "INSERT INTO role_permissions (tenant_id, role, permission) "
                    "SELECT id, %s, %s FROM tenants WHERE slug = %s",
                    (role, permission, tenant),
                )
    print(
        "seeded:", len(TENANTS), "tenants,", len(USERS), "users,", len(ROLE_PERMISSIONS), "grants"
    )


if __name__ == "__main__":
    asyncio.run(seed(os.environ["GATEWAY_MIGRATION_DATABASE_URL"]))
