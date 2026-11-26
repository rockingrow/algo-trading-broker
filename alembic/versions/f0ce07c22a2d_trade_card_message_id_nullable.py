"""trade_notifications.message_id: nullable, for a send Telegram never answered

A card send whose request timed out leaves the DM in the chat with its
``message_id`` unknown. The old column could not hold that, so nothing was
recorded — and the next TRADE event, of which workers emit plenty, posted the
card again, one copy per event. The row is now written with a NULL id, because
its existence is what stops the re-post; such a card cannot be edited, so the
next status change replaces it with a fresh one.

This is the same distinction ``SendOutcome.UNKNOWN`` already encodes for the
channel broadcasts.

Revision ID: f0ce07c22a2d
Revises: 60b00ea13191
Create Date: 2026-11-26 00:00:01.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "f0ce07c22a2d"
down_revision = "60b00ea13191"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.alter_column(
    "trade_notifications",
    "message_id",
    existing_type=sa.Integer(),
    nullable=True,
  )


def downgrade() -> None:
  # Rows recorded for an unanswered send have no id and cannot get one, so they
  # are dropped rather than blocking the column going back to NOT NULL. Their
  # only job was to suppress a duplicate card.
  op.execute("DELETE FROM trade_notifications WHERE message_id IS NULL")
  op.alter_column(
    "trade_notifications",
    "message_id",
    existing_type=sa.Integer(),
    nullable=False,
  )
