"""float to numeric for financial columns

Revision ID: 922c6a79dffc
Revises: 54892682ef32
Create Date: 2026-05-25 00:00:00.000000

"""

from alembic import op

revision = "922c6a79dffc"
down_revision = "54892682ef32"
branch_labels = None
depends_on = None

_PRICE_TYPE = "NUMERIC(20, 8)"
_PCT_TYPE = "NUMERIC(10, 4)"
_USING = "USING {}::numeric"


def upgrade() -> None:
  op.execute(f"""
        ALTER TABLE signals
            ALTER COLUMN price        TYPE {_PRICE_TYPE} {_USING.format("price")},
            ALTER COLUMN quantity     TYPE {_PRICE_TYPE} {_USING.format("quantity")},
            ALTER COLUMN sl           TYPE {_PRICE_TYPE} {_USING.format("sl")},
            ALTER COLUMN tp1          TYPE {_PRICE_TYPE} {_USING.format("tp1")},
            ALTER COLUMN tp2          TYPE {_PRICE_TYPE} {_USING.format("tp2")},
            ALTER COLUMN risk_percent TYPE {_PCT_TYPE}   {_USING.format("risk_percent")};
    """)

  op.execute(f"""
        ALTER TABLE trades
            ALTER COLUMN account_balance_init TYPE {_PRICE_TYPE} {_USING.format("account_balance_init")},
            ALTER COLUMN account_balance      TYPE {_PRICE_TYPE} {_USING.format("account_balance")},
            ALTER COLUMN price                TYPE {_PRICE_TYPE} {_USING.format("price")},
            ALTER COLUMN quantity             TYPE {_PRICE_TYPE} {_USING.format("quantity")},
            ALTER COLUMN sl                   TYPE {_PRICE_TYPE} {_USING.format("sl")},
            ALTER COLUMN tp1                  TYPE {_PRICE_TYPE} {_USING.format("tp1")},
            ALTER COLUMN tp2                  TYPE {_PRICE_TYPE} {_USING.format("tp2")},
            ALTER COLUMN risk_percent         TYPE {_PCT_TYPE}   {_USING.format("risk_percent")};
    """)

  op.execute(f"""
        ALTER TABLE accounts
            ALTER COLUMN account_balance TYPE {_PRICE_TYPE} {_USING.format("account_balance")};
    """)


def downgrade() -> None:
  op.execute("""
        ALTER TABLE signals
            ALTER COLUMN price        TYPE DOUBLE PRECISION USING price::double precision,
            ALTER COLUMN quantity     TYPE DOUBLE PRECISION USING quantity::double precision,
            ALTER COLUMN sl           TYPE DOUBLE PRECISION USING sl::double precision,
            ALTER COLUMN tp1          TYPE DOUBLE PRECISION USING tp1::double precision,
            ALTER COLUMN tp2          TYPE DOUBLE PRECISION USING tp2::double precision,
            ALTER COLUMN risk_percent TYPE DOUBLE PRECISION USING risk_percent::double precision;
    """)

  op.execute("""
        ALTER TABLE trades
            ALTER COLUMN account_balance_init TYPE DOUBLE PRECISION USING account_balance_init::double precision,
            ALTER COLUMN account_balance      TYPE DOUBLE PRECISION USING account_balance::double precision,
            ALTER COLUMN price                TYPE DOUBLE PRECISION USING price::double precision,
            ALTER COLUMN quantity             TYPE DOUBLE PRECISION USING quantity::double precision,
            ALTER COLUMN sl                   TYPE DOUBLE PRECISION USING sl::double precision,
            ALTER COLUMN tp1                  TYPE DOUBLE PRECISION USING tp1::double precision,
            ALTER COLUMN tp2                  TYPE DOUBLE PRECISION USING tp2::double precision,
            ALTER COLUMN risk_percent         TYPE DOUBLE PRECISION USING risk_percent::double precision;
    """)

  op.execute("""
        ALTER TABLE accounts
            ALTER COLUMN account_balance TYPE DOUBLE PRECISION USING account_balance::double precision;
    """)
