"""signals.broadcast_at: tells a resumed fan-out what is still owed

A fan-out has two halves that settle separately. Publishing to the workers is
irrevocable; writing the signal's line onto its broadcast cycle is a database
write that can fail on its own. Until now the row recorded only the pair:
``PUBLISHED`` meant both had happened, so a cycle write that failed left the
row ``QUEUED`` and the whole fan-out — the worker publish included — was run
again on every redelivery, for as long as the write kept failing.

``broadcast_at`` splits the two. ``status=PUBLISHED`` with ``NULL`` here means
"the workers have it, the cycle record is still owed": a redelivery retries
only the cycle write, and publishes nothing a second time.

Rows that were already PUBLISHED are backfilled, because their stored events
predate the ``signal_id`` the cycle de-duplicates on — re-recording one would
append a second line to the message rather than being recognised as a replay.

Revision ID: 125875cbf61f
Revises: f0ce07c22a2d
Create Date: 2026-11-26 00:00:02.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "125875cbf61f"
down_revision = "f0ce07c22a2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "signals",
    sa.Column("broadcast_at", sa.DateTime(timezone=True), nullable=True),
  )
  op.execute(
    """
    UPDATE signals
    SET broadcast_at = COALESCE("updatedAt", "createdAt")
    WHERE status = 'PUBLISHED'
    """
  )


def downgrade() -> None:
  op.drop_column("signals", "broadcast_at")
