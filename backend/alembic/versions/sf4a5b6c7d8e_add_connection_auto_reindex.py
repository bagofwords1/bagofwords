"""add per-connection scheduled auto-reindex columns

Revision ID: sf4a5b6c7d8e
Revises: se3f4a5b6c7d
Create Date: 2026-06-12 10:00:00.000000

Adds per-connection scheduling state for the background schema auto-reload
sweeper (`app.services.scheduled_reindex.sweep_due_reindexes`):

  - auto_reindex_enabled   : per-connection on/off toggle (default on)
  - reindex_interval_hours : per-connection cadence override (NULL -> 12h default)
  - next_retry_at          : failure/backoff gate so a failing source isn't
                             re-kicked every sweep tick
  - last_reindex_error     : last background reindex error (diagnostics)

The feature is gated behind the enterprise `scheduled_reindex` license feature;
the columns themselves are harmless on community installs (the sweeper no-ops
without the license).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'sf4a5b6c7d8e'
down_revision: Union[str, None] = 'se3f4a5b6c7d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows backfill to "enabled" without a data migration.
    op.add_column(
        'connections',
        sa.Column('auto_reindex_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'connections',
        sa.Column('reindex_interval_hours', sa.Integer(), nullable=True),
    )
    op.add_column(
        'connections',
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'connections',
        sa.Column('last_reindex_error', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('connections', 'last_reindex_error')
    op.drop_column('connections', 'next_retry_at')
    op.drop_column('connections', 'reindex_interval_hours')
    op.drop_column('connections', 'auto_reindex_enabled')
