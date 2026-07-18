"""allow multiple accounts per telegram_user_id, add telegram_sessions

Previously ``accounts.telegram_user_id`` was unique, so one Telegram user
could link at most one account. This migration:

- Drops that unique index and recreates it as a plain (non-unique) index —
  one Telegram user may now link several accounts (different market/gateway
  pairs).
- Adds ``telegram_sessions``, one row per Telegram user, tracking which of
  their linked accounts is currently "active" (the one single-account
  commands like /status, /flat act on). ``active_account_id`` is a nullable
  FK to ``accounts.id`` (``ON DELETE SET NULL``).
- Backfills one ``telegram_sessions`` row per currently-linked account. This
  is safe/unambiguous today because the *old* unique constraint guarantees
  at most one account per ``telegram_user_id`` at migration time.

Revision ID: e1f2a3b4c5d6
Revises: c9d8e7f6a5b4
Create Date: 2026-07-19 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # 1. accounts.telegram_user_id: unique -> non-unique index.
  op.execute("DROP INDEX IF EXISTS ix_accounts_telegram_user_id;")
  op.execute(
    "CREATE INDEX IF NOT EXISTS ix_accounts_telegram_user_id "
    "ON accounts (telegram_user_id);"
  )

  # 2. telegram_sessions table.
  op.create_table(
    "telegram_sessions",
    sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
    sa.Column("active_account_id", sa.UUID(), nullable=True),
    sa.Column("id", sa.UUID(), nullable=False),
    sa.Column(
      "createdAt",
      sa.DateTime(timezone=True),
      server_default=sa.text("now()"),
      nullable=False,
    ),
    sa.Column(
      "updatedAt",
      sa.DateTime(timezone=True),
      server_default=sa.text("now()"),
      nullable=False,
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.ForeignKeyConstraint(
      ["active_account_id"], ["accounts.id"], ondelete="SET NULL"
    ),
  )
  op.create_index(
    op.f("ix_telegram_sessions_telegram_user_id"),
    "telegram_sessions",
    ["telegram_user_id"],
    unique=True,
  )

  # 3. Backfill: one session per currently-linked account.
  op.execute("""
        INSERT INTO telegram_sessions (id, telegram_user_id, active_account_id, "createdAt", "updatedAt")
        SELECT gen_random_uuid(), telegram_user_id, id, now(), now()
        FROM accounts
        WHERE telegram_user_id IS NOT NULL;
    """)


def downgrade() -> None:
  # Only safe if no telegram_user_id ended up bound to more than one account
  # row while the non-unique index was in effect — that data would violate
  # the unique index being restored and this will fail.
  op.drop_index(
    op.f("ix_telegram_sessions_telegram_user_id"), table_name="telegram_sessions"
  )
  op.drop_table("telegram_sessions")

  op.execute("DROP INDEX IF EXISTS ix_accounts_telegram_user_id;")
  op.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_telegram_user_id "
    "ON accounts (telegram_user_id);"
  )
