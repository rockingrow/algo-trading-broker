"""add silent_signal and notification_include_signal_raw settings

Revision ID: a4d6e1b9c5f7
Revises: 922c6a79dffc
Create Date: 2026-06-02 00:00:00.000000

"""

from alembic import op

revision = "a4d6e1b9c5f7"
down_revision = "922c6a79dffc"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES
            (gen_random_uuid(), 'silent_signal', '0'),
            (gen_random_uuid(), 'notification_include_signal_raw', '0')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key IN ('silent_signal', 'notification_include_signal_raw');
    """)
