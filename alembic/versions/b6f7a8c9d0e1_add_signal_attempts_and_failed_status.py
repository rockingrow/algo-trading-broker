"""add attempts, last_attempt columns and FAILED status to signals

Revision ID: b6f7a8c9d0e1
Revises: a5e6f7b8c9d0
Create Date: 2026-07-16 01:00:00.000000

"""

from alembic import op

revision = "b6f7a8c9d0e1"
down_revision = "a5e6f7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # Extend the existing signal-status enum so the retry job can terminally
  # mark a row FAILED once every attempt has been exhausted.
  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'signalstatusenum' AND e.enumlabel = 'FAILED'
            ) THEN
                ALTER TYPE signalstatusenum ADD VALUE 'FAILED';
            END IF;
        END $$;
    """)

  # Remaining attempts and the last-attempt timestamp — added together so a
  # partial upgrade never leaves rows queued forever without a way to track
  # their retries. Default 0 for the column, but every new insert overwrites
  # it with settings.SIGNAL_MAX_ATTEMPTS (repository-side); pre-existing rows
  # therefore stop retrying by default until an operator resets them.
  op.execute("""
        ALTER TABLE signals
            ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
    """)
  op.execute("""
        ALTER TABLE signals
            ADD COLUMN IF NOT EXISTS last_attempt TIMESTAMP WITH TIME ZONE;
    """)


def downgrade() -> None:
  op.execute("""
        ALTER TABLE signals
            DROP COLUMN IF EXISTS last_attempt;
    """)
  op.execute("""
        ALTER TABLE signals
            DROP COLUMN IF EXISTS attempts;
    """)
  # Postgres has no DROP VALUE on enum types — dropping FAILED would require
  # recreating the enum. Left in place; harmless if the downgrade is followed
  # by another upgrade.
