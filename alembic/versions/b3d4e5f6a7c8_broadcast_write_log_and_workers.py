"""broadcast write log + CDC trigger, worker table, public chat-ids setting

Three changes that belong together — they are the move from "send Telegram
inside the signal pipeline" to "write to Postgres, let a CDC dispatcher send":

* ``broadcast_message_logs`` — append-only write log. Every change to a cycle
  (a signal, a worker execution) is committed here with the cycle's next
  ``seq`` before anything is sent, and an ``AFTER INSERT`` trigger fires
  ``pg_notify('broadcast_message_log', …)`` so the dispatcher wakes up.
  ``broadcast_messages.last_seq`` holds the counter and
  ``broadcast_message_chats.delivered_seq`` records what each chat has already
  rendered, so a late delivery can never overwrite a newer body.
* ``broadcast_message_workers`` — which workers executed a cycle and their
  latest status, rendered as a table inside the public broadcast message.
* ``public_broadcast_chat_ids`` — the public audience moves from an env var to
  a broker setting so it is editable from the admin API / Telegram bot.

Revision ID: b3d4e5f6a7c8
Revises: a2c3d4e5f6b7
Create Date: 2026-08-16 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, JSONB

revision = "b3d4e5f6a7c8"
down_revision = "a2c3d4e5f6b7"
branch_labels = None
depends_on = None

PUBLIC_BROADCAST_CHAT_IDS_KEY = "public_broadcast_chat_ids"

#: Postgres NOTIFY channel the dispatcher listens on. Kept in sync with
#: ``broker.db.listener.BROADCAST_LOG_CHANNEL``.
CHANNEL = "broadcast_message_log"

log_kind_enum = PgEnum(
  "SIGNAL", "EXECUTION", name="broadcastlogkindenum", create_type=False
)
log_status_enum = PgEnum(
  "PENDING",
  "SENDING",
  "DELIVERED",
  "FAILED",
  name="broadcastlogstatusenum",
  create_type=False,
)

# The payload carries ids only (NOTIFY is capped at 8000 bytes and the
# dispatcher re-reads the row anyway).
NOTIFY_FUNCTION = f"""
CREATE OR REPLACE FUNCTION notify_broadcast_message_log() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify(
    '{CHANNEL}',
    json_build_object(
      'log_id', NEW.id::text,
      'broadcast_message_id', NEW.broadcast_message_id::text,
      'seq', NEW.seq
    )::text
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

NOTIFY_TRIGGER = """
CREATE TRIGGER broadcast_message_logs_notify
AFTER INSERT ON broadcast_message_logs
FOR EACH ROW EXECUTE FUNCTION notify_broadcast_message_log();
"""


def upgrade() -> None:
  bind = op.get_bind()
  log_kind_enum.create(bind, checkfirst=True)
  log_status_enum.create(bind, checkfirst=True)

  op.add_column(
    "broadcast_messages",
    sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
  )
  op.add_column(
    "broadcast_message_chats",
    sa.Column("delivered_seq", sa.BigInteger(), server_default="0", nullable=False),
  )

  op.create_table(
    "broadcast_message_workers",
    sa.Column("broadcast_message_id", sa.UUID(), nullable=False),
    sa.Column("worker_id", sa.String(length=128), nullable=False),
    sa.Column("account_id", sa.String(length=50), nullable=False),
    sa.Column(
      "market", PgEnum(name="markettypeenum", create_type=False), nullable=True
    ),
    sa.Column("gateway", sa.String(length=50), nullable=True),
    sa.Column(
      "latest_status",
      PgEnum(name="tradestatusenum", create_type=False),
      nullable=False,
    ),
    sa.Column("latest_action", sa.String(length=20), nullable=True),
    sa.Column("reject_reason", sa.String(length=255), nullable=True),
    sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
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
      "worker_id",
      name="uq_broadcast_message_workers_message_worker",
    ),
  )
  op.create_index(
    op.f("ix_broadcast_message_workers_broadcast_message_id"),
    "broadcast_message_workers",
    ["broadcast_message_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_broadcast_message_workers_worker_id"),
    "broadcast_message_workers",
    ["worker_id"],
    unique=False,
  )

  op.create_table(
    "broadcast_message_logs",
    sa.Column("broadcast_message_id", sa.UUID(), nullable=False),
    sa.Column("seq", sa.BigInteger(), nullable=False),
    sa.Column("kind", log_kind_enum, nullable=False),
    sa.Column("payload", JSONB(), nullable=True),
    sa.Column("status", log_status_enum, server_default="PENDING", nullable=False),
    sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    sa.Column("last_error", sa.String(length=255), nullable=True),
    sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
      "broadcast_message_id", "seq", name="uq_broadcast_message_logs_message_seq"
    ),
  )
  op.create_index(
    op.f("ix_broadcast_message_logs_broadcast_message_id"),
    "broadcast_message_logs",
    ["broadcast_message_id"],
    unique=False,
  )
  op.create_index(
    op.f("ix_broadcast_message_logs_status"),
    "broadcast_message_logs",
    ["status"],
    unique=False,
  )

  op.execute(NOTIFY_FUNCTION)
  op.execute(NOTIFY_TRIGGER)

  # Seed the setting (empty = public broadcast off) so it is visible, and
  # editable, before it is ever set.
  op.execute(f"""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), '{PUBLIC_BROADCAST_CHAT_IDS_KEY}', '')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute(f"""
        DELETE FROM broker_settings
        WHERE key = '{PUBLIC_BROADCAST_CHAT_IDS_KEY}';
    """)

  op.execute(
    "DROP TRIGGER IF EXISTS broadcast_message_logs_notify ON broadcast_message_logs;"
  )
  op.execute("DROP FUNCTION IF EXISTS notify_broadcast_message_log();")

  op.drop_index(
    op.f("ix_broadcast_message_logs_status"), table_name="broadcast_message_logs"
  )
  op.drop_index(
    op.f("ix_broadcast_message_logs_broadcast_message_id"),
    table_name="broadcast_message_logs",
  )
  op.drop_table("broadcast_message_logs")

  op.drop_index(
    op.f("ix_broadcast_message_workers_worker_id"),
    table_name="broadcast_message_workers",
  )
  op.drop_index(
    op.f("ix_broadcast_message_workers_broadcast_message_id"),
    table_name="broadcast_message_workers",
  )
  op.drop_table("broadcast_message_workers")

  op.drop_column("broadcast_message_chats", "delivered_seq")
  op.drop_column("broadcast_messages", "last_seq")

  bind = op.get_bind()
  log_status_enum.drop(bind, checkfirst=True)
  log_kind_enum.drop(bind, checkfirst=True)
