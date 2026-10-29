"""Per-agent routine selection

Revision ID: routine02
Revises: routine01
Create Date: 2026-10-29

Adds `data_sources.active_routine_ids` (JSON, nullable): the ConnectionRoutine
ids this agent exposes to the model. NULL = none active (default). Additive only.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "routine02"
down_revision: str | None = "routine01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if _has_column("data_sources", "active_routine_ids"):
        return
    op.add_column("data_sources", sa.Column("active_routine_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    if not _has_column("data_sources", "active_routine_ids"):
        return
    op.drop_column("data_sources", "active_routine_ids")
