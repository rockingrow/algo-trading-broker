"""add crypto_allowed_symbol setting

Revision ID: c1a2b3d4e5f6
Revises: b3f2a1d0e5c6
Create Date: 2026-06-30 00:00:00.000000

"""

from alembic import op

revision = "c1a2b3d4e5f6"
down_revision = "b3f2a1d0e5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'crypto_allowed_symbol', 'BTC,ETH')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key = 'crypto_allowed_symbol';
    """)
