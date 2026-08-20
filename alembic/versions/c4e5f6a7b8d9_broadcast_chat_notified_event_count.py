"""broadcast reply notices: track what each chat has been told about

A cycle's Telegram message is edited in place as the trade progresses, which
means a reader who already saw it is never told that anything changed. Each new
event now also gets a short two-line reply under that same message, and this
column records how many of the cycle's events a chat has already been notified
about so a redelivery (or the sweeper) cannot post the same notice twice.

NULL is deliberate: rows written before this migration have no idea what they
announced, so the dispatcher backfills them to the cycle's current event count
on the next delivery instead of replying once per past event.

Revision ID: c4e5f6a7b8d9
Revises: b3d4e5f6a7c8
Create Date: 2026-08-20 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "c4e5f6a7b8d9"
down_revision = "b3d4e5f6a7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "broadcast_message_chats",
    sa.Column("notified_event_count", sa.Integer(), nullable=True),
  )


def downgrade() -> None:
  op.drop_column("broadcast_message_chats", "notified_event_count")
