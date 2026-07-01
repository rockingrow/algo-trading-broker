"""add telegram_user_id and telegram_link_token to accounts

Revision ID: d4f1a2c3b5e6
Revises: b3f2a1d0e5c6
Create Date: 2026-06-29 00:00:00.000000

"""

from alembic import op

revision = "d4f1a2c3b5e6"
down_revision = "b3f2a1d0e5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # Add the columns (idempotent).
  op.execute("""
        ALTER TABLE accounts
            ADD COLUMN IF NOT EXISTS telegram_user_id    BIGINT,
            ADD COLUMN IF NOT EXISTS telegram_link_token UUID;
    """)

  # Backfill a link token for every existing account so admins always have a
  # UUID to hand out. gen_random_uuid() ships with PostgreSQL 13+ core.
  op.execute("""
        UPDATE accounts
            SET telegram_link_token = gen_random_uuid()
            WHERE telegram_link_token IS NULL;
    """)

  # Unique indexes mirror the ORM's ``unique=True, index=True`` columns.
  # NULLs are considered distinct in Postgres, so multiple unbound accounts
  # (telegram_user_id IS NULL) remain valid.
  op.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_telegram_user_id "
    "ON accounts (telegram_user_id);"
  )
  op.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_telegram_link_token "
    "ON accounts (telegram_link_token);"
  )


def downgrade() -> None:
  op.execute("DROP INDEX IF EXISTS ix_accounts_telegram_link_token;")
  op.execute("DROP INDEX IF EXISTS ix_accounts_telegram_user_id;")
  op.execute("""
        ALTER TABLE accounts
            DROP COLUMN IF EXISTS telegram_link_token,
            DROP COLUMN IF EXISTS telegram_user_id;
    """)
