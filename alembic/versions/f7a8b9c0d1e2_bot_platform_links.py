"""decouple bot/chat-platform bindings from accounts

``accounts`` previously carried two Telegram-specific columns,
``telegram_user_id`` and ``telegram_link_token``. That coupled a trading
domain table to one chat platform, and — being scalar — could not express an
account managed by more than one person: a second link overwrote the first.

This migration introduces the platform-generic replacement:

- ``account_bot_links`` — many-to-many between accounts and bot users, keyed by
  ``(platform, platform_user_id, account_id)``. No role column yet; every
  linked user has the same rights.
- ``account_link_tokens`` — invite secrets, several per account, individually
  revocable (``revoked_at``) and optionally time-boxed (``expires_at``, NULL =
  never expires).
- ``bot_sessions`` — which of a bot user's linked accounts is currently active.
  Replaces ``telegram_sessions``.

``platform_user_id`` is VARCHAR(64) rather than BIGINT: Telegram/Discord ids
are numeric but Slack/Matrix ids are opaque strings, and widening later would
cost another migration.

There is no backfill from the old columns — they were never deployed with real
data. A link token is seeded for every existing account so admins always have a
UUID to hand out, matching the old ``default=uuid.uuid4`` behaviour.

Revision ID: f7a8b9c0d1e2
Revises: c9d8e7f6a5b4
Create Date: 2026-07-20 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision = "f7a8b9c0d1e2"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def _timestamp_columns() -> list[sa.Column]:
  """The ``Base`` mixin columns every table in this schema carries."""
  return [
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
  ]


def upgrade() -> None:
  op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'botplatformtypeenum') THEN
                CREATE TYPE botplatformtypeenum AS ENUM ('TELEGRAM');
            END IF;
        END $$;
    """)

  op.create_table(
    "account_bot_links",
    sa.Column("account_id", sa.UUID(), nullable=False),
    sa.Column(
      "platform", PgEnum(name="botplatformtypeenum", create_type=False), nullable=False
    ),
    sa.Column("platform_user_id", sa.String(length=64), nullable=False),
    *_timestamp_columns(),
    sa.PrimaryKeyConstraint("id"),
    sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
    sa.UniqueConstraint(
      "platform",
      "platform_user_id",
      "account_id",
      name="uq_account_bot_links_platform_user_account",
    ),
  )
  op.create_index(
    op.f("ix_account_bot_links_account_id"),
    "account_bot_links",
    ["account_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_account_bot_links_platform_user_id"),
    "account_bot_links",
    ["platform_user_id"],
    unique=False,
  )

  op.create_table(
    "account_link_tokens",
    sa.Column("account_id", sa.UUID(), nullable=False),
    sa.Column("token", sa.UUID(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    *_timestamp_columns(),
    sa.PrimaryKeyConstraint("id"),
    sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
  )
  op.create_index(
    op.f("ix_account_link_tokens_account_id"),
    "account_link_tokens",
    ["account_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_account_link_tokens_token"), "account_link_tokens", ["token"], unique=True
  )

  op.create_table(
    "bot_sessions",
    sa.Column(
      "platform", PgEnum(name="botplatformtypeenum", create_type=False), nullable=False
    ),
    sa.Column("platform_user_id", sa.String(length=64), nullable=False),
    sa.Column("active_account_id", sa.UUID(), nullable=True),
    *_timestamp_columns(),
    sa.PrimaryKeyConstraint("id"),
    sa.ForeignKeyConstraint(["active_account_id"], ["accounts.id"], ondelete="SET NULL"),
    sa.UniqueConstraint(
      "platform", "platform_user_id", name="uq_bot_sessions_platform_user"
    ),
  )
  op.create_index(
    op.f("ix_bot_sessions_platform_user_id"),
    "bot_sessions",
    ["platform_user_id"],
    unique=False,
  )

  # Seed one never-expiring token per existing account.
  op.execute("""
        INSERT INTO account_link_tokens (id, account_id, token, "createdAt", "updatedAt")
        SELECT gen_random_uuid(), id, gen_random_uuid(), now(), now()
        FROM accounts;
    """)


def downgrade() -> None:
  op.drop_index(op.f("ix_bot_sessions_platform_user_id"), table_name="bot_sessions")
  op.drop_table("bot_sessions")
  op.drop_index(op.f("ix_account_link_tokens_token"), table_name="account_link_tokens")
  op.drop_index(
    op.f("ix_account_link_tokens_account_id"), table_name="account_link_tokens"
  )
  op.drop_table("account_link_tokens")
  op.drop_index(
    op.f("ix_account_bot_links_platform_user_id"), table_name="account_bot_links"
  )
  op.drop_index(op.f("ix_account_bot_links_account_id"), table_name="account_bot_links")
  op.drop_table("account_bot_links")
  op.execute("DROP TYPE IF EXISTS botplatformtypeenum;")
