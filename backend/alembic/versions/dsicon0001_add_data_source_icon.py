"""add icon to data_sources

Stores an optional per-agent custom icon override as a namespaced token
("emoji:<grapheme>" today; "preset:<key>" reserved for later). NULL means no
override — the UI falls back to the connection type / connector icon.

Revision ID: dsicon0001
Revises: connratelimit01
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa


revision = "dsicon0001"
down_revision = "connratelimit01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_sources", sa.Column("icon", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_sources", "icon")
