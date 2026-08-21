"""add settings column to accounts

Adds the per-account ``settings`` JSONB blob holding what a bot user set with a
command (today ``signal_blocked``, written by /prevent and /allow). The broker
sends it to the worker in the ``settings`` block of its WORKER_CONNECTED_ACK.

JSONB rather than a column per toggle so a new command costs no migration, and
so a single key can be merged server-side (``settings || '{"k": v}'``) instead
of read-modify-written. NOT NULL with a ``{}`` default: an account that has
never run a command reads as "no settings", never as NULL. Postgres 11+ stores
a non-volatile default in the catalog, so adding the column does not rewrite
the table.

Revision ID: a7b8c9d0e1f2
Revises: b3d4e5f6a7c8
Create Date: 2026-08-11 00:00:00.000000

"""

from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "b3d4e5f6a7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        ALTER TABLE accounts
            ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;
    """)


def downgrade() -> None:
  op.execute("""
        ALTER TABLE accounts
            DROP COLUMN IF EXISTS settings;
    """)
