"""trade cards are opted in per account link, not per user

The live-trade-card opt-in used to be one row per bot user in
``trade_broadcast_subscriptions``, so ``/subscribe`` switched cards on for every
account the user held. It is now a flag on the link itself —
``account_bot_links.trade_cards_enabled`` — and ``/subscribe`` /
``/unsubscribe`` flip it for the user's *active* account only.

The flag defaults to false and nothing is backfilled: every existing
subscription is dropped with its table, and each user subscribes again, account
by account. Unlinking deletes the link row, so the preference no longer outlives
the access it belonged to.

The downgrade recreates ``trade_broadcast_subscriptions`` empty — a per-account
flag cannot be folded back into a per-user row without guessing which account
the user meant — and drops the column.

Revision ID: 486671e7d451
Revises: d4e5f6a7b8c9
Create Date: 2026-10-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision = "486671e7d451"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "account_bot_links",
    sa.Column(
      "trade_cards_enabled",
      sa.Boolean(),
      server_default=sa.text("false"),
      nullable=False,
    ),
  )
  op.drop_index(
    op.f("ix_trade_broadcast_subscriptions_platform_user_id"),
    table_name="trade_broadcast_subscriptions",
  )
  op.drop_table("trade_broadcast_subscriptions")


def downgrade() -> None:
  op.create_table(
    "trade_broadcast_subscriptions",
    sa.Column(
      "platform", PgEnum(name="botplatformtypeenum", create_type=False), nullable=False
    ),
    sa.Column("platform_user_id", sa.String(length=64), nullable=False),
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
    sa.UniqueConstraint(
      "platform",
      "platform_user_id",
      name="uq_trade_broadcast_subscriptions_platform_user",
    ),
  )
  op.create_index(
    op.f("ix_trade_broadcast_subscriptions_platform_user_id"),
    "trade_broadcast_subscriptions",
    ["platform_user_id"],
    unique=False,
  )
  op.drop_column("account_bot_links", "trade_cards_enabled")
