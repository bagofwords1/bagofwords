"""Per-dashboard "Made with BOW" switch

Revision ID: credit01
Revises: embedmrg01
Create Date: 2026-10-30

* ``artifacts.bow_credit``: show "Made with BOW" on this dashboard's pages
  (default on). The organization's ``general.bow_credit`` still turns it off
  everywhere.

Additive and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "credit01"
down_revision: str | None = "embedmrg01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    return "bow_credit" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("artifacts")}


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("bow_credit", sa.Boolean(), nullable=False, server_default="1"))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("bow_credit")
