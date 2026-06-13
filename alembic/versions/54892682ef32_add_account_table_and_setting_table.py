"""add account table and setting table

Revision ID: 54892682ef32
Revises: 0001
Create Date: 2026-05-19 23:19:43.692570

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum


revision = "54892682ef32"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'markettypeenum') THEN
                CREATE TYPE markettypeenum AS ENUM ('FOREX', 'CRYPTO');
            END IF;
        END $$;
    """)

  bind = op.get_bind()
  inspector = sa.inspect(bind)
  existing_tables = inspector.get_table_names()

  if "accounts" not in existing_tables:
    op.create_table(
      "accounts",
      sa.Column("account_id", sa.String(length=50), nullable=False),
      sa.Column("account_name", sa.String(length=255), nullable=True),
      sa.Column("account_balance", sa.Float(), nullable=True),
      sa.Column(
        "market_type", PgEnum(name="markettypeenum", create_type=False), nullable=False
      ),
      sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
      sa.Column("id", sa.UUID(), nullable=False),
      sa.Column(
        "createdAt",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
      ),
      sa.Column(
        "updatedAt",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
      ),
      sa.PrimaryKeyConstraint("id"),
      sa.UniqueConstraint("account_id", name="uq_accounts_account_id"),
    )
    op.create_index(
      op.f("ix_accounts_account_id"), "accounts", ["account_id"], unique=False
    )

  if "broker_settings" not in existing_tables:
    op.create_table(
      "broker_settings",
      sa.Column("key", sa.String(length=255), nullable=False),
      sa.Column("value", sa.String(length=255), nullable=False),
      sa.Column("id", sa.UUID(), nullable=False),
      sa.Column(
        "createdAt",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
      ),
      sa.Column(
        "updatedAt",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
      ),
      sa.PrimaryKeyConstraint("id"),
      sa.UniqueConstraint("key", name="uq_broker_settings_key"),
    )
    op.create_index(
      op.f("ix_broker_settings_key"), "broker_settings", ["key"], unique=False
    )

  # Migrate signals indexes from raw-SQL names to alembic-convention names
  existing_indexes = {idx["name"] for idx in inspector.get_indexes("signals")}
  if "idx_signals_symbol" in existing_indexes:
    op.drop_index("idx_signals_symbol", table_name="signals")
  if "idx_signals_timestamp" in existing_indexes:
    op.drop_index("idx_signals_timestamp", table_name="signals")
  if "ix_signals_symbol" not in existing_indexes:
    op.create_index(op.f("ix_signals_symbol"), "signals", ["symbol"], unique=False)

  # Migrate trades indexes from raw-SQL names to alembic-convention names
  existing_trade_indexes = {idx["name"] for idx in inspector.get_indexes("trades")}
  existing_trade_constraints = {
    c["name"] for c in inspector.get_unique_constraints("trades")
  }

  if "idx_trades_account_id" in existing_trade_indexes:
    op.drop_index("idx_trades_account_id", table_name="trades")
  if "idx_trades_strategy_code" in existing_trade_indexes:
    op.drop_index("idx_trades_strategy_code", table_name="trades")
  if "idx_trades_symbol" in existing_trade_indexes:
    op.drop_index("idx_trades_symbol", table_name="trades")
  if "uq_trades_account_ref_id" in existing_trade_indexes:
    op.drop_index("uq_trades_account_ref_id", table_name="trades")
  if "uq_trades_account_ref_id" not in existing_trade_constraints:
    op.create_unique_constraint(
      "uq_trades_account_ref_id", "trades", ["account_id", "ref_id"]
    )
  if "ix_trades_account_id" not in existing_trade_indexes:
    op.create_index(
      op.f("ix_trades_account_id"), "trades", ["account_id"], unique=False
    )
  if "ix_trades_strategy_code" not in existing_trade_indexes:
    op.create_index(
      op.f("ix_trades_strategy_code"), "trades", ["strategy_code"], unique=False
    )
  if "ix_trades_symbol" not in existing_trade_indexes:
    op.create_index(op.f("ix_trades_symbol"), "trades", ["symbol"], unique=False)
  if "ix_trades_ref_id" not in existing_trade_indexes:
    op.create_index(op.f("ix_trades_ref_id"), "trades", ["ref_id"], unique=False)

  op.execute("""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'prevent_signal', '0')
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
  op.drop_index(op.f("ix_trades_ref_id"), table_name="trades")
  op.drop_index(op.f("ix_trades_symbol"), table_name="trades")
  op.drop_index(op.f("ix_trades_strategy_code"), table_name="trades")
  op.drop_index(op.f("ix_trades_account_id"), table_name="trades")
  op.drop_constraint("uq_trades_account_ref_id", "trades", type_="unique")
  op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_account_ref_id
            ON trades (account_id, ref_id);
    """)
  op.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol        ON trades (symbol);")
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy_code ON trades (strategy_code);"
  )
  op.execute(
    "CREATE INDEX IF NOT EXISTS idx_trades_account_id    ON trades (account_id);"
  )
  op.drop_index(op.f("ix_signals_symbol"), table_name="signals")
  op.execute(
    'CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals ("timestamp");'
  )
  op.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol    ON signals (symbol);")
  op.drop_index(op.f("ix_broker_settings_key"), table_name="broker_settings")
  op.drop_table("broker_settings")
  op.drop_index(op.f("ix_accounts_account_id"), table_name="accounts")
  op.drop_table("accounts")
  op.execute("DROP TYPE IF EXISTS markettypeenum;")
