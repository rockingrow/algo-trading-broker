"""symbol in the broadcast cycle key

One ``symbol`` + ``strategy`` pair now runs several signals at the same time,
each carrying its own ``signal_uxid`` and its own full entry-to-exit action
trail. The cycle key therefore has to name the symbol as well: keyed on
``(strategy, signal_uxid)`` alone, a ``signal_uxid`` minted for one symbol
would fold another symbol's cycle into the same row — and into the same
Telegram message.

This migration replaces ``broadcast_messages``' unique constraint
``(strategy, signal_uxid)`` with ``(symbol, strategy, signal_uxid)``. The new
constraint is strictly weaker than the old one, so no existing row can violate
it and no data has to be rewritten.

Revision ID: d4e5f6a7b8c9
Revises: 0205b8abba12
Create Date: 2026-09-26 00:00:00.000000

"""

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "0205b8abba12"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.drop_constraint(
    "uq_broadcast_messages_strategy_signal_uxid",
    "broadcast_messages",
    type_="unique",
  )
  op.create_unique_constraint(
    "uq_broadcast_messages_symbol_strategy_signal_uxid",
    "broadcast_messages",
    ["symbol", "strategy", "signal_uxid"],
  )


def downgrade() -> None:
  # Only safe while no two cycles share a (strategy, signal_uxid) across two
  # symbols — rows created under the composite key may, and then restoring the
  # narrower constraint fails rather than silently merging them.
  op.drop_constraint(
    "uq_broadcast_messages_symbol_strategy_signal_uxid",
    "broadcast_messages",
    type_="unique",
  )
  op.create_unique_constraint(
    "uq_broadcast_messages_strategy_signal_uxid",
    "broadcast_messages",
    ["strategy", "signal_uxid"],
  )
