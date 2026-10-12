"""Admin CLI (owner DB role). Pins backend tool definitions into PostgreSQL.

uv run python -m scripts.registry_admin sync                  # pin new tools, report drift
uv run python -m scripts.registry_admin sync --approve-changes  # also accept changed tools
uv run python -m scripts.registry_admin status
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from shared.auth.internal import InternalTokenSigner, load_private_key_b64
from shared.registry.admin import fetch_live_tools, sync_backend
from shared.registry.manifest import Manifest

load_dotenv()


async def cmd_sync(args: argparse.Namespace) -> int:
    manifest = Manifest.from_toml(args.manifest)
    # Backends only accept gateway-signed tokens, so the operator tool signs a discovery token too.
    signer = InternalTokenSigner(
        load_private_key_b64(os.environ["GATEWAY_INTERNAL_PRIVATE_KEY_B64"])
    )
    exit_code = 0
    async with await AsyncConnection.connect(
        os.environ["GATEWAY_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as conn:
        for backend in manifest.backends:
            try:
                token = signer.mint(audience=backend.audience, principal=None)
                live = await fetch_live_tools(
                    backend.url, headers={"Authorization": f"Bearer {token}"}
                )
            except Exception as exc:
                print(f"[{backend.name}] cannot reach backend: {type(exc).__name__}")
                exit_code = 1
                continue
            async with conn.transaction():
                r = await sync_backend(conn, backend, live, approve_changes=args.approve_changes)
            print(f"[{r.backend}] new={r.new} unchanged={r.unchanged} changed={r.changed}")
            if r.unregistered:
                print(f"  not in manifest (NOT exposed): {r.unregistered}")
            if r.missing:
                print(f"  in manifest but not served by backend: {r.missing}")
            if r.changed and not args.approve_changes:
                print(
                    "  DRIFT: definitions changed since pinning.\n"
                    "  Review the change, then re-run with --approve-changes."
                )
                exit_code = 2
    return exit_code


async def cmd_status(_: argparse.Namespace) -> int:
    async with await AsyncConnection.connect(
        os.environ["GATEWAY_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as conn:
        rows = await (
            await conn.execute(
                """SELECT s.name AS server, t.name, t.permission, left(t.schema_hash, 12) AS hash,
                          t.enabled, t.pinned_at
                   FROM mcp_tools t JOIN mcp_servers s ON s.id = t.server_id
                   ORDER BY 1, 2"""
            )
        ).fetchall()
    for r in rows:
        print(
            f"{r['server']}.{r['name']:<18} {r['permission']:<16} {r['hash']} "
            f"enabled={r['enabled']}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sync = sub.add_parser("sync")
    sync.add_argument("--manifest", default="config/registry.toml")
    sync.add_argument("--approve-changes", action="store_true")
    sync.set_defaults(fn=cmd_sync)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = parser.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
