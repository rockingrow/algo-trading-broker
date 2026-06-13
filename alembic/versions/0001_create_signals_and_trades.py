"""create signals and trades tables

Revision ID: 0001
Revises:
Create Date: 2026-05-19 00:00:00.000000

"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
  # ── Shared enum & trigger function ───────────────────────────────────────

  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'signalactionenum') THEN
                CREATE TYPE signalactionenum AS ENUM (
                    'LONG', 'SHORT', 'TP1', 'TP2', 'R_SL', 'SL', 'FLAT'
                );
            END IF;
        END $$;
    """)

  op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW."updatedAt" = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE 'plpgsql';
    """)

  # ── signals ───────────────────────────────────────────────────────────────

  op.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

            symbol      VARCHAR(50)  NOT NULL,
            timeframe   VARCHAR(20)  NOT NULL,
            "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL,

            strategy     VARCHAR(50)       NOT NULL DEFAULT 'gold',
            action       signalactionenum  NOT NULL,
            price        DOUBLE PRECISION  NOT NULL,
            quantity     DOUBLE PRECISION  NOT NULL,
            sl           DOUBLE PRECISION,
            tp1          DOUBLE PRECISION,
            tp2          DOUBLE PRECISION,
            is_running   BOOLEAN           NOT NULL DEFAULT FALSE,
            risk_percent DOUBLE PRECISION  NOT NULL DEFAULT 0.0,

            indicators JSONB,
            inputs     JSONB,
            raw        JSONB
        );
    """)

  op.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol    ON signals (symbol);")
  op.execute(
    'CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals ("timestamp");'
  )

  op.execute("DROP TRIGGER IF EXISTS trg_update_signals_updated_at ON signals;")
  op.execute("""
        CREATE TRIGGER trg_update_signals_updated_at
            BEFORE UPDATE ON signals
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)

  # ── trades ────────────────────────────────────────────────────────────────

  op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tradestatusenum') THEN
                CREATE TYPE tradestatusenum AS ENUM (
                    'OPENED', 'REJECTED', 'PARTIALLY_CLOSED', 'CLOSED', 'FLAT'
                );
            END IF;
        END $$;
    """)

  op.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,

            account_id           VARCHAR(50)  NOT NULL,
            account_leverage     INTEGER      NOT NULL,
            account_balance_init DOUBLE PRECISION,
            account_balance      DOUBLE PRECISION,

            ref_id VARCHAR(255),
            comment       VARCHAR(255),
            strategy_code VARCHAR(255) NOT NULL,

            strategy     VARCHAR(50)       NOT NULL DEFAULT 'gold',
            symbol       VARCHAR(50)       NOT NULL,
            action       signalactionenum  NOT NULL,
            price        DOUBLE PRECISION  NOT NULL,
            quantity     DOUBLE PRECISION  NOT NULL,
            sl           DOUBLE PRECISION,
            tp1          DOUBLE PRECISION,
            tp2          DOUBLE PRECISION,
            is_running   BOOLEAN           NOT NULL DEFAULT FALSE,
            risk_percent DOUBLE PRECISION  NOT NULL DEFAULT 0.0,

            gateway_return_code INTEGER,

            status        tradestatusenum NOT NULL,
            reject_reason VARCHAR(255)
        );
    """)

  op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_account_ref_id
            ON trades (account_id, ref_id);
    """)
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_trades_account_id    ON trades (account_id);"
  )
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy_code ON trades (strategy_code);"
  )
  op.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades (symbol);")

  op.execute("DROP TRIGGER IF EXISTS trg_update_trades_updated_at ON trades;")
  op.execute("""
        CREATE TRIGGER trg_update_trades_updated_at
            BEFORE UPDATE ON trades
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)


def downgrade() -> None:
  op.execute("DROP TRIGGER IF EXISTS trg_update_trades_updated_at ON trades;")
  op.execute("DROP TABLE IF EXISTS trades;")
  op.execute("DROP TYPE IF EXISTS tradestatusenum;")

  op.execute("DROP TRIGGER IF EXISTS trg_update_signals_updated_at ON signals;")
  op.execute("DROP TABLE IF EXISTS signals;")

  # CASCADE removes any remaining trigger dependencies
  op.execute("DROP FUNCTION IF EXISTS update_updated_at_column() CASCADE;")
  op.execute("DROP TYPE IF EXISTS signalactionenum;")
