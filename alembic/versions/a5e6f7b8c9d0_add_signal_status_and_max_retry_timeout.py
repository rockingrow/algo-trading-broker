"""add signal status column and max_retry_timeout setting

Revision ID: a5e6f7b8c9d0
Revises: f4d5e6a7b8c9
Create Date: 2026-07-16 00:00:00.000000

"""

from alembic import op

revision = "a5e6f7b8c9d0"
down_revision = "f4d5e6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'signalstatusenum') THEN
                CREATE TYPE signalstatusenum AS ENUM ('QUEUED', 'PUBLISHED');
            END IF;
        END $$;
    """)

  op.execute("""
        ALTER TABLE signals
            ADD COLUMN IF NOT EXISTS status signalstatusenum
                NOT NULL DEFAULT 'QUEUED';
    """)

  op.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);")

  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'max_retry_timeout', '60')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key = 'max_retry_timeout';
    """)

  op.execute("DROP INDEX IF EXISTS idx_signals_status;")

  op.execute("""
        ALTER TABLE signals
            DROP COLUMN IF EXISTS status;
    """)

  op.execute("DROP TYPE IF EXISTS signalstatusenum;")
