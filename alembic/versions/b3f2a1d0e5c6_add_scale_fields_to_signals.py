"""add is_scale_position and scale_strategy to signals

Revision ID: b3f2a1d0e5c6
Revises: a4d6e1b9c5f7
Create Date: 2026-06-26 00:00:00.000000

"""

from alembic import op

revision = "b3f2a1d0e5c6"
down_revision = "a4d6e1b9c5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        ALTER TABLE signals
            ADD COLUMN IF NOT EXISTS is_scale_position BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS scale_strategy     VARCHAR(50);
    """)


def downgrade() -> None:
  op.execute("""
        ALTER TABLE signals
            DROP COLUMN IF EXISTS is_scale_position,
            DROP COLUMN IF EXISTS scale_strategy;
    """)
