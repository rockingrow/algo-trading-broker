"""merge heads + composite (market, gateway, account_id) key on accounts,
add market/gateway to trades

Two accounts on different gateways may coincidentally share the same bare
``account_id`` (e.g. MT5 login "100234" and a Binance account "100234" are
unrelated real accounts). Previously ``accounts.account_id`` alone was unique,
which made that combination impossible to register. This migration:

- Adds ``market``/``gateway`` to ``trades`` (nullable, backfilled from the
  owning ``accounts`` row while ``account_id`` was still globally unique) so a
  trade can be attributed to the right account once ``account_id`` alone no
  longer is.
- Replaces ``accounts``' single-column unique constraint with a composite one
  on ``(market, gateway, account_id)``.
- Replaces ``trades``' ``(account_id, ref_id)`` unique constraint with
  ``(market, gateway, account_id, ref_id)``.

KNOWN LIMITATION (not fixed by this migration): admin-facing lookups that take
a bare ``account_id`` — ``rotate_link_token``, ``list_by_account``/
``count_by_account`` (``/trades``, ``/atrades``), and the admin ``FLAT``
broadcast's optional ``account_id`` scope — still resolve/match by
``account_id`` alone and can be ambiguous if it collides across gateways.
Worse, the admin FLAT signal is broadcast on one shared NATS ADMIN subject to
every worker (``broker/services/nats_service.py::publish_admin_signal``); each
worker filters for its own ``account_id`` client-side, in worker code that
lives outside this repo. If two workers on different gateways share a bare
``account_id``, an admin FLAT meant for one can also be actioned by the other.
Until the worker side disambiguates by ``market``+``gateway`` too, avoid
deliberately reusing an ``account_id`` across gateways in production.

Revision ID: c9d8e7f6a5b4
Revises: b6f7a8c9d0e1, d4f1a2c3b5e6
Create Date: 2026-07-19 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision = "c9d8e7f6a5b4"
down_revision = ("b6f7a8c9d0e1", "d4f1a2c3b5e6")
branch_labels = None
depends_on = None


def upgrade() -> None:
  # 1. Add + backfill trades.market/gateway while accounts.account_id is
  #    still 1:1 with a single accounts row (the old constraint is dropped in
  #    step 3, below). Nullable: a handful of orphan trade rows with no
  #    matching accounts row (if any) simply keep NULL rather than blocking
  #    the migration.
  op.add_column(
    "trades",
    sa.Column(
      "market", PgEnum(name="markettypeenum", create_type=False), nullable=True
    ),
  )
  op.add_column("trades", sa.Column("gateway", sa.String(length=50), nullable=True))

  op.execute("""
        UPDATE trades t
        SET market = a.market,
            gateway = a.gateway
        FROM accounts a
        WHERE t.account_id = a.account_id
          AND t.market IS NULL;
    """)

  # 2. trades: (account_id, ref_id) -> (market, gateway, account_id, ref_id)
  op.drop_constraint("uq_trades_account_ref_id", "trades", type_="unique")
  op.create_unique_constraint(
    "uq_trades_market_gateway_account_ref",
    "trades",
    ["market", "gateway", "account_id", "ref_id"],
  )

  # 3. accounts: account_id -> (market, gateway, account_id)
  op.drop_constraint("uq_accounts_account_id", "accounts", type_="unique")
  op.create_unique_constraint(
    "uq_accounts_market_gateway_account_id",
    "accounts",
    ["market", "gateway", "account_id"],
  )


def downgrade() -> None:
  # Only safe if no two rows were created that share an account_id across
  # gateways while the composite constraint was in effect — that data would
  # violate the single-column constraint being restored and this will fail.
  op.drop_constraint(
    "uq_accounts_market_gateway_account_id", "accounts", type_="unique"
  )
  op.create_unique_constraint("uq_accounts_account_id", "accounts", ["account_id"])

  op.drop_constraint("uq_trades_market_gateway_account_ref", "trades", type_="unique")
  op.create_unique_constraint(
    "uq_trades_account_ref_id", "trades", ["account_id", "ref_id"]
  )

  op.drop_column("trades", "gateway")
  op.drop_column("trades", "market")
