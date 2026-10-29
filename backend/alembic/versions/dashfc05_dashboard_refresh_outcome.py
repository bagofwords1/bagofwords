"""Dashboards as first-class entities: how the last refresh went

Revision ID: dashfc05
Revises: dashfc04
Create Date: 2026-10-15

Phase 6 of docs/design/dashboards-first-class.md. A published dashboard
refreshes on its schedule, when opened and on the owner's Refresh, all of
its copies or none. `artifacts.last_run_at` is when the last refresh
succeeded; `last_run_status` ('success' | 'failed') and `last_run_error`
say how the last attempt went, so the owner sees "the last refresh failed,
and why" while readers keep the previous rows.

The downgrade drops both columns: they describe the last attempt only.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc05"
down_revision: str | None = "dashfc04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("last_run_status", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("last_run_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("last_run_error")
        batch.drop_column("last_run_status")
