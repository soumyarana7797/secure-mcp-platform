"""allow 'rate_limited' audit events

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_event_type_check
            CHECK (event_type IN ('tool_call', 'auth_failed', 'rate_limited'));
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM audit_events WHERE event_type = 'rate_limited';
        ALTER TABLE audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_event_type_check
            CHECK (event_type IN ('tool_call', 'auth_failed'));
    """)
