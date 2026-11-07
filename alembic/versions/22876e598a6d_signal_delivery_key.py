"""signals.delivery_key: one row per JetStream message, however often delivered

JetStream redelivers a ``SIGNALS`` message whose ack never reached it — the
broker crashed after the fan-out, or the ack was lost — and every redelivery
used to insert a fresh row with a fresh ``signal_id``. Workers de-duplicate on
``signal_id``, so the same alert could open a second position. The message's
identity (stream sequence + store time) is now stored on the row under a
unique constraint, and a redelivery reuses the row it finds.

Nullable and not backfilled: rows written before this revision have no key and
are never redelivered again anyway (their messages were acked long ago).

Revision ID: 22876e598a6d
Revises: 486671e7d451
Create Date: 2026-11-06 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "22876e598a6d"
down_revision = "486671e7d451"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "signals",
    sa.Column("delivery_key", sa.String(length=128), nullable=True),
  )
  op.create_unique_constraint("uq_signals_delivery_key", "signals", ["delivery_key"])


def downgrade() -> None:
  op.drop_constraint("uq_signals_delivery_key", "signals", type_="unique")
  op.drop_column("signals", "delivery_key")
