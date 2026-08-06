"""add strategy_magic_map setting

Seeds the ``strategy_magic_map`` broker setting: a JSON-text object mapping each
strategy to its magic number. The broker sends it to every worker as a
STRATEGY_MAGIC_MAP message on connect, filtered down to the strategies that
worker announced.

Revision ID: f1a2b3c4d5e6
Revises: a1b2c3d4e5f6
Create Date: 2026-08-06 00:00:00.000000

"""

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


# Stored verbatim as text and parsed as JSON at read time (see
# broker.services.nats_service._parse_strategy_magic_map). Keys are strategy
# names, values are the magic numbers workers key their orders by.
STRATEGY_MAGIC_MAP_VALUE = (
  '{"HOLD_TO_WIN": 9999}'
)


def upgrade() -> None:
  op.execute(
    f"""
        INSERT INTO broker_settings (id, key, value)
        VALUES (gen_random_uuid(), 'strategy_magic_map', '{STRATEGY_MAGIC_MAP_VALUE}')
        ON CONFLICT (key) DO NOTHING;
    """
  )


def downgrade() -> None:
  op.execute("""
        DELETE FROM broker_settings
        WHERE key = 'strategy_magic_map';
    """)
