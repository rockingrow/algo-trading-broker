-- Migration: Create SignalLog table
-- Description: Creates the signal_log table and associated Enum type for audit logging.
-- Run this after PostgreSQL is installed and started.

-- 1. Create the action enum type for TradingView signals
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'signalactionenum') THEN
        CREATE TYPE signalactionenum AS ENUM ('LONG', 'SHORT', 'TP1', 'TP2', 'R_SL', 'SL');
    END IF;
END $$;

-- 2. Create the signal_log table
CREATE TABLE IF NOT EXISTS signal_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- WebhookPayload columns
    symbol VARCHAR(50) NOT NULL,
    timeframe VARCHAR(20) NOT NULL,
    "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL,
    
    -- PositionSchema columns
    action signalactionenum NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    sl DOUBLE PRECISION,
    tp1 DOUBLE PRECISION,
    tp2 DOUBLE PRECISION,
    is_running BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Complex objects stored as JSONB
    indicators JSONB NOT NULL,
    inputs JSONB NOT NULL
);

-- 3. Create indices for performance
CREATE INDEX IF NOT EXISTS idx_signal_log_symbol ON signal_log (symbol);
CREATE INDEX IF NOT EXISTS idx_signal_log_timestamp ON signal_log ("timestamp");

-- 4. Set up auto-update for updatedAt column
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW."updatedAt" = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Drop trigger if exists to avoid errors on re-run
DROP TRIGGER IF EXISTS trg_update_signal_log_updated_at ON signal_log;

CREATE TRIGGER trg_update_signal_log_updated_at
    BEFORE UPDATE ON signal_log
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
