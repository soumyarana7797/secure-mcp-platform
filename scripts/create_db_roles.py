"""Creates/updates the per-service database roles on an EXISTING database (owner role).

Fresh volumes get them from docker/postgres/init.sh. Idempotent: safe to run repeatedly.
Passwords come from the environment and are never printed.
"""

import asyncio
import os

from dotenv import load_dotenv
from psycopg import AsyncConnection, sql

load_dotenv()

ROLES = {"user_svc": "USER_SVC_PASSWORD", "knowledge_svc": "KNOWLEDGE_SVC_PASSWORD"}


async def main() -> None:
    async with await AsyncConnection.connect(
        os.environ["GATEWAY_MIGRATION_DATABASE_URL"], autocommit=True
    ) as conn:
        for role, env_var in ROLES.items():
            password = os.environ[env_var]
            exists = await (
                await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            ).fetchone()
            verb = "ALTER" if exists else "CREATE"
            await conn.execute(
                sql.SQL(
                    "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(password))
            )
            print(f"{verb.lower()}d role {role}")


if __name__ == "__main__":
    asyncio.run(main())
