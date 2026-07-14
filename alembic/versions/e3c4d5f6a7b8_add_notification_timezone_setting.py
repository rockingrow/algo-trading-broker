"""add notification_timezone setting

Revision ID: e3c4d5f6a7b8
Revises: d2b3c4e5f6a7
Create Date: 2026-07-06 00:00:00.000000

"""

from alembic import op

revision = "e3c4d5f6a7b8"
down_revision = "d2b3c4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'notification_timezone', '7')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key = 'notification_timezone';
    """)
