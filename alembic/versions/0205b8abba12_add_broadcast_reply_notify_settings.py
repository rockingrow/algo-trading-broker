"""add private/public broadcast reply notify settings

Revision ID: 0205b8abba12
Revises: b8c9d0e1f2a3
Create Date: 2026-08-21 00:00:00.000000

"""

from alembic import op

revision = "0205b8abba12"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES
            (gen_random_uuid(), 'private_broadcast_reply_notify', '1'),
            (gen_random_uuid(), 'public_broadcast_reply_notify', '1')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key IN ('private_broadcast_reply_notify', 'public_broadcast_reply_notify');
    """)
