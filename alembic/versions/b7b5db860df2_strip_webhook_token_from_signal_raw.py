"""signals.raw: strip the webhook token from every stored payload

``signals.raw`` kept the whole ``WebhookPayload``, ``token`` included — and the
token *is* ``WEBHOOK_SECRET``. Every row therefore held the key to the webhook,
readable by anyone with access to the database or one of its backups. New rows
are written without it (``SqlAlchemySignalRepository.log_signal``) and readers
rebuild the payload with ``payload_from_stored``; this revision removes it from
the rows already written.

Data only, no DDL. The downgrade is a no-op on purpose: the secret cannot be
recovered, and must not be put back.

Revision ID: b7b5db860df2
Revises: 22876e598a6d
Create Date: 2026-11-25 00:00:00.000000

"""

from alembic import op

revision = "b7b5db860df2"
down_revision = "22876e598a6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
  # ``raw -> 'token' IS NOT NULL`` selects every row that has the key, a JSON
  # null value included (jsonb 'null' is not SQL NULL), so nothing is skipped.
  op.execute("UPDATE signals SET raw = raw - 'token' WHERE raw -> 'token' IS NOT NULL")


def downgrade() -> None:
  pass
