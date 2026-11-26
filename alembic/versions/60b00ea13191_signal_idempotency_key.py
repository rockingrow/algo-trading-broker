"""signals.idempotency_key: one row per alert, however many messages carry it

``delivery_key`` (revision 22876e598a6d) recognises the same *stored* JetStream
message coming back. It cannot recognise one alert arriving as two separate
stored messages — a producer re-publishing to ``SIGNALS.<strategy>``, a replayed
request — and each of those minted its own ``signal_id``, which is precisely
what a worker de-duplicates on, so the alert was executed twice. Meanwhile the
broadcast layer's own content check kept the Telegram message showing a single
line, so nothing about it was visible.

The key is a SHA-256 over the payload with ``token`` excluded and object keys
sorted (``signal_helper.payload_idempotency_key``), under its own unique
constraint, so the second insert loses and the handler reuses the row already
written.

Nullable and not backfilled: computing it for historical rows would need the
same canonical form their ``raw`` no longer guarantees, and an equality lookup
never matches NULL, so old rows are simply invisible to it.

Revision ID: 60b00ea13191
Revises: b7b5db860df2
Create Date: 2026-11-26 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "60b00ea13191"
down_revision = "b7b5db860df2"
branch_labels = None
depends_on = None


def upgrade() -> None:
  op.add_column(
    "signals",
    sa.Column("idempotency_key", sa.String(length=64), nullable=True),
  )
  op.create_unique_constraint(
    "uq_signals_idempotency_key", "signals", ["idempotency_key"]
  )


def downgrade() -> None:
  op.drop_constraint("uq_signals_idempotency_key", "signals", type_="unique")
  op.drop_column("signals", "idempotency_key")
