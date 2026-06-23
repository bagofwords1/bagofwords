"""add reindex schedule (interval-minutes / fixed time) columns

Revision ID: tz1a2b3c4d5e
Revises: a1c2m0de9f01
Create Date: 2026-06-23 10:00:00.000000

Extends the per-connection scheduled auto-reindex (enterprise `scheduled_reindex`)
so an admin can choose EITHER a recurring interval OR a fixed time-of-day:

  - reindex_schedule_mode    : "interval" | "time" (default "interval")
  - reindex_interval_minutes : interval cadence in minutes (10m floor); supersedes
                               the legacy `reindex_interval_hours` column
  - reindex_at_time          : "HH:MM" daily fire time, interpreted in the org tz

Existing rows backfill: mode -> "interval"; reindex_interval_minutes derived from
the legacy hours column (NULL stays NULL -> default cadence applies).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'tz1a2b3c4d5e'
down_revision: Union[str, None] = 'a1c2m0de9f01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'connections',
        sa.Column('reindex_schedule_mode', sa.String(), nullable=False,
                  server_default='interval'),
    )
    op.add_column(
        'connections',
        sa.Column('reindex_interval_minutes', sa.Integer(), nullable=True),
    )
    op.add_column(
        'connections',
        sa.Column('reindex_at_time', sa.String(), nullable=True),
    )
    # Backfill minutes from the legacy hours column where present.
    op.execute(
        "UPDATE connections SET reindex_interval_minutes = reindex_interval_hours * 60 "
        "WHERE reindex_interval_hours IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column('connections', 'reindex_at_time')
    op.drop_column('connections', 'reindex_interval_minutes')
    op.drop_column('connections', 'reindex_schedule_mode')
