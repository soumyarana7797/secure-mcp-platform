"""least-privilege grants for the user service role

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'user_svc') THEN
        RAISE EXCEPTION 'role user_svc is missing: run `uv run python -m scripts.create_db_roles`';
    END IF;
END $$;

-- Column-level grants: the user service can read profile fields but NOT cognito_sub, and it can
-- see nothing else (no audit, permissions, registry). Row-level security still applies.
GRANT SELECT (id, tenant_id, email, display_name, roles, status) ON users TO user_svc;
GRANT SELECT (id, slug, name) ON tenants TO user_svc;
"""

DOWNGRADE = """
REVOKE SELECT ON users, tenants FROM user_svc;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
