"""add crypto_max_leverage setting

Revision ID: d2b3c4e5f6a7
Revises: c1a2b3d4e5f6
Create Date: 2026-06-30 00:00:01.000000

"""

from alembic import op

revision = "d2b3c4e5f6a7"
down_revision = "c1a2b3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'crypto_max_leverage', '10')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key = 'crypto_max_leverage';
    """)
