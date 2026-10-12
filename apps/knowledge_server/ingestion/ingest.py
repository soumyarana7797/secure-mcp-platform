"""Ingest documents for one tenant (admin action; uses the OWNER database role)."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from apps.knowledge_server.ingestion.chunking import chunk_text
from shared.embeddings.base import Embedder


@dataclass
class IngestResult:
    document_id: UUID | None
    chunks: int
    skipped: bool  # identical content was already ingested for this tenant


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


async def ingest_document(
    conn: AsyncConnection,
    tenant_id: UUID,
    title: str,
    source: str,
    text: str,
    embedder: Embedder,
) -> IngestResult:
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    row = await (
        await conn.execute(
            "INSERT INTO documents (tenant_id, title, source, content_hash) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, content_hash) DO NOTHING RETURNING id",
            (tenant_id, title, source, content_hash),
        )
    ).fetchone()
    if row is None:
        return IngestResult(None, 0, skipped=True)
    document_id = row["id"]
    chunks = chunk_text(text)
    vectors = embedder.embed(chunks)
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO chunks (document_id, tenant_id, ordinal, content, embedder, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s::vector)",
            [
                (document_id, tenant_id, i, chunk, embedder.id, vector_literal(vec))
                for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )
    return IngestResult(document_id, len(chunks), skipped=False)
