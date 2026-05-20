"""create accounts and broker_settings tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-19 00:00:02.000000

"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # 1. markettypeenum type
  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'markettypeenum') THEN
                CREATE TYPE markettypeenum AS ENUM ('FOREX', 'CRYPTO');
            END IF;
        END $$;
    """)

  # 2. accounts table
  op.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

            account_id      VARCHAR(50)      NOT NULL,
            account_name    VARCHAR(255),
            account_balance DOUBLE PRECISION,
            market_type     markettypeenum   NOT NULL,
            last_activity_at TIMESTAMP WITH TIME ZONE
        );
    """)

  # 3. accounts indices
  op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_account_id ON accounts (account_id);
    """)
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_accounts_account_id ON accounts (account_id);"
  )

  # 4. accounts updatedAt trigger
  op.execute("DROP TRIGGER IF EXISTS trg_update_accounts_updated_at ON accounts;")
  op.execute("""
        CREATE TRIGGER trg_update_accounts_updated_at
            BEFORE UPDATE ON accounts
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)

  # 5. broker_settings table
  op.execute("""
        CREATE TABLE IF NOT EXISTS broker_settings (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

            key   VARCHAR(255) NOT NULL,
            value VARCHAR(255) NOT NULL
        );
    """)

  # 6. broker_settings indices
  op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_broker_settings_key ON broker_settings (key);
    """)
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_broker_settings_key ON broker_settings (key);"
  )

  # 7. broker_settings updatedAt trigger
  op.execute(
    "DROP TRIGGER IF EXISTS trg_update_broker_settings_updated_at ON broker_settings;"
  )
  op.execute("""
        CREATE TRIGGER trg_update_broker_settings_updated_at
            BEFORE UPDATE ON broker_settings
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)

  # 8. seed default settings
  op.execute("""
        INSERT INTO broker_settings (key, value)
        VALUES ('prevent_signal', '0')
        ON CONFLICT DO NOTHING;
    """)


def downgrade() -> None:
  op.execute(
    "DROP TRIGGER IF EXISTS trg_update_broker_settings_updated_at ON broker_settings;"
  )
  op.execute("DROP TABLE IF EXISTS broker_settings;")
  op.execute("DROP TRIGGER IF EXISTS trg_update_accounts_updated_at ON accounts;")
  op.execute("DROP TABLE IF EXISTS accounts;")
  op.execute("DROP TYPE IF EXISTS markettypeenum;")
