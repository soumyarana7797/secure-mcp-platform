"""Ingest markdown files for one tenant into the knowledge base (OWNER database role).

    uv run python -m scripts.ingest_docs --tenant acme --dir data/sample_docs/acme
Re-running is safe: documents with identical content are skipped.
"""

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from apps.knowledge_server.ingestion.ingest import ingest_document
from shared.embeddings.hashing import HashingEmbedder

load_dotenv()


async def main(tenant: str, directory: Path) -> int:
    embedder = HashingEmbedder()
    async with await AsyncConnection.connect(
        os.environ["GATEWAY_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as conn:
        row = await (
            await conn.execute("SELECT id FROM tenants WHERE slug = %s", (tenant,))
        ).fetchone()
        if row is None:
            print(f"unknown tenant: {tenant}")
            return 1
        for path in sorted(directory.glob("*.md")):
            text = path.read_text()
            title = next(
                (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")),
                path.stem,
            )
            async with conn.transaction():
                result = await ingest_document(conn, row["id"], title, path.name, text, embedder)
            status = "skipped (unchanged)" if result.skipped else f"{result.chunks} chunk(s)"
            print(f"[{tenant}] {path.name}: {status}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.tenant, args.dir)))
