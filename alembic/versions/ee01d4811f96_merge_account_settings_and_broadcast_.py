"""merge account settings and broadcast chat notified event count heads

Revision ID: ee01d4811f96
Revises: a7b8c9d0e1f2, c4e5f6a7b8d9
Create Date: 2026-08-20 20:42:37.473726

"""
from alembic import op
import sqlalchemy as sa


revision = 'ee01d4811f96'
down_revision = ('a7b8c9d0e1f2', 'c4e5f6a7b8d9')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
