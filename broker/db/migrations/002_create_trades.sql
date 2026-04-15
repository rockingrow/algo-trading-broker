-- Migration: Create Trades table
-- Description: Creates the trades table and associated Enum type for trade statuses.

-- 1. Create the status enum type for Trade
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
    
    -- Foreign key to signals
    signal_id UUID NOT NULL,
    
    -- Trading Account info
    account_id VARCHAR(50) NOT NULL,
    account_leverage INTEGER NOT NULL,
    account_balance_init DOUBLE PRECISION,
    account_balance DOUBLE PRECISION,
    
    -- Broker-specific fields
    ticket DOUBLE PRECISION,
    comment VARCHAR(255),
    magic VARCHAR(50) NOT NULL,
    
    -- Trade details
    symbol VARCHAR(50) NOT NULL,
    action signalactionenum NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    sl DOUBLE PRECISION,
    tp1 DOUBLE PRECISION,
    tp2 DOUBLE PRECISION,
    is_running BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Status
    status tradestatusenum NOT NULL
    );

-- 3. Create indices for performance
CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades (signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_account_id ON trades (account_id);
CREATE INDEX IF NOT EXISTS idx_trades_magic ON trades (magic);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);

-- 4. Set up auto-update for updatedAt column
-- (Function update_updated_at_column() should already exist from 001_create_signals.sql)

-- Drop trigger if exists to avoid errors on re-run
DROP TRIGGER IF EXISTS trg_update_trades_updated_at ON trades;

CREATE TRIGGER trg_update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
