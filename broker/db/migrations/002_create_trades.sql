-- Migration: Create trades table
-- Description: Creates the trades table and the tradestatusenum type.

-- 1. Create the tradestatusenum type
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tradestatusenum') THEN
        CREATE TYPE tradestatusenum AS ENUM ('OPENED', 'REJECTED', 'PARTIALLY_CLOSED', 'CLOSED');
    END IF;
END $$;

-- 2. Create the trades table
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    signal_id UUID NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    account_leverage INTEGER NOT NULL,
    account_balance_init DOUBLE PRECISION,
    account_balance DOUBLE PRECISION,
    
    ticket DOUBLE PRECISION,
    comment VARCHAR(255),
    magic VARCHAR(50) NOT NULL,
    
    symbol VARCHAR(50) NOT NULL,
    action signalactionenum NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    sl DOUBLE PRECISION,
    tp1 DOUBLE PRECISION,
    tp2 DOUBLE PRECISION,
    is_running BOOLEAN NOT NULL DEFAULT FALSE,
    status tradestatusenum NOT NULL,
    risk_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

-- 3. Create indices for trades
CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades (signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_account_id ON trades (account_id);
CREATE INDEX IF NOT EXISTS idx_trades_magic ON trades (magic);

-- 4. Set up auto-update for updatedAt column on trades
DROP TRIGGER IF EXISTS trg_update_trades_updated_at ON trades;
CREATE TRIGGER trg_update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
