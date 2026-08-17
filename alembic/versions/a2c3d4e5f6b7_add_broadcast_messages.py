"""add broadcast_messages + broadcast_message_chats, and signals.signal_uxid

One Telegram message per *signal cycle* instead of one per signal:
``broadcast_messages`` holds the cycle (unique on ``strategy`` +
``signal_uxid``) and ``broadcast_message_chats`` holds the message id and the
rendered body for each chat the cycle was broadcast to, so a follow-up signal
edits those messages instead of posting new ones.

``signals.signal_uxid`` records the same cycle id on the audit row.

Revision ID: a2c3d4e5f6b7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-15 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, JSONB

revision = "a2c3d4e5f6b7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


# Created explicitly (create_type=False on the columns) so the enums exist
# before either table references them, and so downgrade can drop them.
broadcast_status_enum = PgEnum(
  "RUNNING", "CLOSED", name="broadcaststatusenum", create_type=False
)
broadcast_audience_enum = PgEnum(
  "PRIVATE", "PUBLIC", name="broadcastaudienceenum", create_type=False
)


def upgrade() -> None:
  bind = op.get_bind()
  broadcast_status_enum.create(bind, checkfirst=True)
  broadcast_audience_enum.create(bind, checkfirst=True)

  op.add_column(
    "signals", sa.Column("signal_uxid", sa.String(length=16), nullable=True)
  )
  op.create_index(
    op.f("ix_signals_signal_uxid"), "signals", ["signal_uxid"], unique=False
  )

  op.create_table(
    "broadcast_messages",
    sa.Column("strategy", sa.String(length=50), nullable=False),
    sa.Column("signal_uxid", sa.String(length=16), nullable=False),
    sa.Column("symbol", sa.String(length=50), nullable=False),
    sa.Column("timeframe", sa.String(length=20), nullable=True),
    sa.Column("actions", sa.Text(), nullable=False),
    sa.Column(
      "latest_action",
      PgEnum(name="signalactionenum", create_type=False),
      nullable=False,
    ),
    sa.Column(
      "status",
      broadcast_status_enum,
      server_default="RUNNING",
      nullable=False,
    ),
    sa.Column("events", JSONB(), nullable=False),
    sa.Column("last_broadcast_at", sa.DateTime(timezone=True), nullable=True),
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
      "strategy", "signal_uxid", name="uq_broadcast_messages_strategy_signal_uxid"
    ),
  )
  op.create_index(
    op.f("ix_broadcast_messages_strategy"),
    "broadcast_messages",
    ["strategy"],
    unique=False,
  )
  op.create_index(
    op.f("ix_broadcast_messages_signal_uxid"),
    "broadcast_messages",
    ["signal_uxid"],
    unique=False,
  )
  op.create_index(
    op.f("ix_broadcast_messages_status"),
    "broadcast_messages",
    ["status"],
    unique=False,
  )

  op.create_table(
    "broadcast_message_chats",
    sa.Column("broadcast_message_id", sa.UUID(), nullable=False),
    sa.Column("audience", broadcast_audience_enum, nullable=False),
    sa.Column("chat_id", sa.String(length=64), nullable=False),
    sa.Column("message_id", sa.String(length=64), nullable=True),
    sa.Column("message", sa.Text(), nullable=True),
    sa.Column("last_error", sa.String(length=255), nullable=True),
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
    sa.ForeignKeyConstraint(
      ["broadcast_message_id"], ["broadcast_messages.id"], ondelete="CASCADE"
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint(
      "broadcast_message_id",
      "chat_id",
      name="uq_broadcast_message_chats_message_chat",
    ),
  )
  op.create_index(
    op.f("ix_broadcast_message_chats_broadcast_message_id"),
    "broadcast_message_chats",
    ["broadcast_message_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_broadcast_message_chats_chat_id"),
    "broadcast_message_chats",
    ["chat_id"],
    unique=False,
  )


def downgrade() -> None:
  op.drop_index(
    op.f("ix_broadcast_message_chats_chat_id"), table_name="broadcast_message_chats"
  )
  op.drop_index(
    op.f("ix_broadcast_message_chats_broadcast_message_id"),
    table_name="broadcast_message_chats",
  )
  op.drop_table("broadcast_message_chats")

  op.drop_index(op.f("ix_broadcast_messages_status"), table_name="broadcast_messages")
  op.drop_index(
    op.f("ix_broadcast_messages_signal_uxid"), table_name="broadcast_messages"
  )
  op.drop_index(op.f("ix_broadcast_messages_strategy"), table_name="broadcast_messages")
  op.drop_table("broadcast_messages")

  op.drop_index(op.f("ix_signals_signal_uxid"), table_name="signals")
  op.drop_column("signals", "signal_uxid")

  bind = op.get_bind()
  broadcast_audience_enum.drop(bind, checkfirst=True)
  broadcast_status_enum.drop(bind, checkfirst=True)
