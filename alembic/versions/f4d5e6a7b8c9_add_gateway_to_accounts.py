"""add gateway column to accounts

Revision ID: f4d5e6a7b8c9
Revises: e3c4d5f6a7b8
Create Date: 2026-07-09 00:00:00.000000

"""

from alembic import op

revision = "f4d5e6a7b8c9"
down_revision = "e3c4d5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        ALTER TABLE accounts
            ADD COLUMN IF NOT EXISTS gateway VARCHAR(50);
    """)


def downgrade() -> None:
  op.execute("""
        ALTER TABLE accounts
            DROP COLUMN IF EXISTS gateway;
    """)
