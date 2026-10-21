"""add TP1 status to the broadcast-cycle status enum

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-20 09:00:00.000000

Apply this **before** restarting the broker on the code that writes the new
label. A broker running ahead of the migration fails the status write with an
invalid-enum error, and ``record_event`` rolls the whole transaction back —
including the appended event — so the TP1 line would be missing for good from
a message body that is rebuilt from the stored events.

"""

from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # A partial take-profit leaves the position open, so it is not a close — but
  # the broadcast header has to say the trade reached TP1 instead of still
  # reading RUNNING. Inserted BEFORE 'CLOSED' so the enum's own order matches
  # the order a cycle walks (RUNNING -> TP1 -> CLOSED), which is what
  # broker/domain/broadcast_status.py enforces.
  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'broadcaststatusenum' AND e.enumlabel = 'TP1'
            ) THEN
                ALTER TYPE broadcaststatusenum ADD VALUE 'TP1' BEFORE 'CLOSED';
            END IF;
        END $$;
    """)


def downgrade() -> None:
  # Cycles already marked TP1 are live trades: downgrading the label would have
  # to pick RUNNING (hides the partial fill) or CLOSED (claims a trade is out),
  # so they are moved back to RUNNING, which is what every revision before this
  # one wrote for a TP1 anyway.
  op.execute("""
        UPDATE broadcast_messages SET status = 'RUNNING' WHERE status = 'TP1';
    """)
  # Postgres has no DROP VALUE on enum types — dropping TP1 would require
  # recreating the enum. Left in place; harmless if the downgrade is followed
  # by another upgrade.
