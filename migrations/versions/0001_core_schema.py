"""core schema: tenants, users, registry, permissions, row-level security

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# The tenant the current transaction acts for. Unset or empty => NULL => RLS matches no rows.
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

UPGRADE = f"""
CREATE TABLE tenants (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{{2,40}}$'),
    name       text NOT NULL,
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id),
    cognito_sub  text NOT NULL UNIQUE,
    email        text,
    display_name text,
    roles        text[] NOT NULL DEFAULT '{{}}',
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX users_tenant_idx ON users (tenant_id);

CREATE TABLE role_permissions (
    tenant_id  uuid NOT NULL REFERENCES tenants(id),
    role       text NOT NULL,
    permission text NOT NULL CHECK (permission ~ '^[a-z_]+:[a-z_]+$'),
    PRIMARY KEY (tenant_id, role, permission)
);

CREATE TABLE mcp_servers (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9_]{{0,31}}$'),
    url        text NOT NULL,
    audience   text NOT NULL,
    timeout_s  numeric NOT NULL DEFAULT 5 CHECK (timeout_s > 0 AND timeout_s <= 120),
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- "Pinned" tool definitions: what an admin reviewed and approved. The gateway serves THESE
-- descriptions/schemas, and refuses a tool whose live schema no longer matches schema_hash.
CREATE TABLE mcp_tools (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    server_id    uuid NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name         text NOT NULL,
    description  text NOT NULL,
    input_schema jsonb NOT NULL,
    schema_hash  text NOT NULL,
    permission   text NOT NULL CHECK (permission ~ '^[a-z_]+:[a-z_]+$'),
    timeout_s    numeric CHECK (timeout_s > 0 AND timeout_s <= 120),
    annotations  jsonb,
    enabled      boolean NOT NULL DEFAULT true,
    pinned_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (server_id, name)
);

-- Row-level security: tenant-owned tables only match the tenant set for this transaction.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants USING (id = {CURRENT_TENANT});

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON users USING (tenant_id = {CURRENT_TENANT});

ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON role_permissions USING (tenant_id = {CURRENT_TENANT});

-- Login must find a user BEFORE the tenant is known, which RLS would block. A narrowly scoped
-- SECURITY DEFINER function (runs with the owner's rights) returns exactly one active identity.
CREATE FUNCTION resolve_principal(p_sub text)
RETURNS TABLE (user_id uuid, tenant_id uuid, tenant_slug text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.tenant_id, t.slug, u.roles
    FROM users u JOIN tenants t ON t.id = u.tenant_id
    WHERE u.cognito_sub = p_sub AND u.status = 'active' AND t.status = 'active'
$$;
REVOKE ALL ON FUNCTION resolve_principal(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_principal(text) TO gateway_app;

-- The runtime role gets the minimum: read-only. Writes happen via the admin CLI (owner role).
GRANT SELECT ON tenants, users, role_permissions, mcp_servers, mcp_tools TO gateway_app;
"""

DOWNGRADE = """
DROP FUNCTION IF EXISTS resolve_principal(text);
DROP TABLE IF EXISTS mcp_tools, mcp_servers, role_permissions, users, tenants CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
