"""audit_events (append-only, tenant-scoped) and rate_limit_policies

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

UPGRADE = f"""
CREATE TABLE audit_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    request_id    text,
    event_type    text NOT NULL CHECK (event_type IN ('tool_call', 'auth_failed')),
    tenant_id     uuid REFERENCES tenants(id),   -- NULL: caller was never authenticated
    user_id       uuid,
    subject       text,
    tool          text,
    decision      text NOT NULL CHECK (decision IN ('allow', 'deny')),
    outcome       text NOT NULL,
    latency_ms    numeric,
    args_hash     text,
    redacted_args jsonb
);
CREATE INDEX audit_events_tenant_ts_idx ON audit_events (tenant_id, ts DESC);
CREATE INDEX audit_events_ts_idx ON audit_events (ts DESC);

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
-- A tenant reads only its own trail; anonymous (NULL tenant) events are readable by no tenant.
CREATE POLICY tenant_read ON audit_events FOR SELECT USING (tenant_id = {CURRENT_TENANT});
-- Writes: either an anonymous event, or one for exactly the tenant this transaction acts for.
CREATE POLICY tenant_write ON audit_events FOR INSERT
    WITH CHECK (tenant_id IS NULL OR tenant_id = {CURRENT_TENANT});

-- Append-only, even for the table owner. Retention is done by dropping partitions or TRUNCATE.
CREATE FUNCTION audit_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END
$$;
CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();

GRANT INSERT, SELECT ON audit_events TO gateway_app;   -- no UPDATE, no DELETE

-- NULL tenant = platform default. A tenant-specific row overrides it.
CREATE TABLE rate_limit_policies (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid REFERENCES tenants(id),
    scope               text NOT NULL CHECK (scope IN ('tenant', 'user')),
    requests_per_minute integer NOT NULL CHECK (requests_per_minute > 0),
    burst               integer NOT NULL CHECK (burst > 0),
    UNIQUE NULLS NOT DISTINCT (tenant_id, scope)
);
GRANT SELECT ON rate_limit_policies TO gateway_app;     -- platform config, no tenant secrets
INSERT INTO rate_limit_policies (tenant_id, scope, requests_per_minute, burst) VALUES
    (NULL, 'tenant', 600, 100),
    (NULL, 'user',   120, 30);
"""

DOWNGRADE = """
DROP TABLE IF EXISTS rate_limit_policies;
DROP TABLE IF EXISTS audit_events;
DROP FUNCTION IF EXISTS audit_events_immutable();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
