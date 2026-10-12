"""knowledge base: documents and embedded chunks (pgvector), tenant-isolated

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

UPGRADE = f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'knowledge_svc') THEN
        RAISE EXCEPTION 'role knowledge_svc missing: run scripts.create_db_roles';
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id),
    title        text NOT NULL,
    source       text NOT NULL,
    content_hash text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, content_hash)         -- re-ingesting the same text is a no-op
);

CREATE TABLE chunks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id   uuid NOT NULL REFERENCES tenants(id),   -- denormalised so RLS needs no join
    ordinal     integer NOT NULL,
    content     text NOT NULL,
    embedder    text NOT NULL,                -- which model produced the vector; never mix models
    embedding   vector(4096) NOT NULL,
    UNIQUE (document_id, ordinal)
);
CREATE INDEX chunks_tenant_idx ON chunks (tenant_id, embedder);
-- No ANN index on purpose: exact search is fast at this scale and approximate (HNSW) indexes can
-- return too few rows once a per-tenant filter is applied.

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON documents USING (tenant_id = {CURRENT_TENANT});
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks USING (tenant_id = {CURRENT_TENANT});

-- Search is read-only. Ingestion is an admin action done with the owner role.
GRANT SELECT ON documents, chunks TO knowledge_svc;
"""

DOWNGRADE = """
DROP TABLE IF EXISTS chunks, documents;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
