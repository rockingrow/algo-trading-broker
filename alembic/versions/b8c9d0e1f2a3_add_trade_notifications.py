"""add trade_notifications table and trades.last_action

``trade_notifications`` remembers the Telegram message that carries a trade's
live card for one subscriber, so later status changes edit that message instead
of posting a new one. One row per ``(trade_id, platform, chat_id)``; rows
cascade away with their trade.

``trades.last_action`` records the event that last moved a trade (TP1/TP2/SL/
R_SL/FLAT/...). Several of those persist as the same ``status`` and ``action``
keeps the entry direction, so without it a re-rendered card can never say how a
trade ended. Nullable — existing rows have none.

Revision ID: b8c9d0e1f2a3
Revises: ee01d4811f96
Create Date: 2026-08-20 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision = "b8c9d0e1f2a3"
down_revision = "ee01d4811f96"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column("trades", sa.Column("last_action", sa.String(length=20), nullable=True))
  op.create_table(
    "trade_notifications",
    sa.Column("trade_id", sa.UUID(), nullable=False),
    sa.Column(
      "platform", PgEnum(name="botplatformtypeenum", create_type=False), nullable=False
    ),
    sa.Column("chat_id", sa.String(length=64), nullable=False),
    sa.Column("message_id", sa.Integer(), nullable=False),
    sa.Column(
      "status", PgEnum(name="tradestatusenum", create_type=False), nullable=False
    ),
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
    sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint(
      "trade_id",
      "platform",
      "chat_id",
      name="uq_trade_notifications_trade_platform_chat",
    ),
  )
  op.create_index(
    op.f("ix_trade_notifications_trade_id"),
    "trade_notifications",
    ["trade_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_trade_notifications_chat_id"),
    "trade_notifications",
    ["chat_id"],
    unique=False,
  )


def downgrade() -> None:
  op.drop_index(
    op.f("ix_trade_notifications_chat_id"), table_name="trade_notifications"
  )
  op.drop_index(
    op.f("ix_trade_notifications_trade_id"), table_name="trade_notifications"
  )
  op.drop_table("trade_notifications")
  op.drop_column("trades", "last_action")
