"""add trade_broadcast_subscriptions table

Per-user opt-in for completed-trade Telegram broadcasts. A row exists for a
``(platform, platform_user_id)`` while that bot user wants a DM whenever one of
their linked accounts closes a trade; unsubscribing deletes it.

Revision ID: a1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-07-19 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision = "a1b2c3d4e5f6"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
  op.drop_index(
    op.f("ix_trade_broadcast_subscriptions_platform_user_id"),
    table_name="trade_broadcast_subscriptions",
  )
  op.drop_table("trade_broadcast_subscriptions")
