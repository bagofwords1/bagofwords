"""add per-connection request rate limit (enterprise)

Revision ID: connratelimit01
Revises: m1e2r3g4e5f6
Create Date: 2026-07-09 10:00:00.000000

Adds the per-connection request rate limit (enterprise `connection_rate_limit`):

  - connections.rate_limit_enabled     : per-connection on/off toggle (default off)
  - connections.rate_limit_per_minute  : requests/minute cap (NULL/0 -> no limit)
  - connections.rate_limit_per_hour    : requests/hour cap   (NULL/0 -> no limit)
  - connections.rate_limit_per_day     : requests/day cap    (NULL/0 -> no limit)

  - connection_rate_limit_counters     : fixed-window request counters, one row
                                         per (connection, window, bucket). The
                                         only shared store in the stack is
                                         Postgres (no Redis), so window counting
                                         lives in the DB.

The feature is gated behind the enterprise license; the columns/table are
harmless on community installs (the limiter no-ops without the feature).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'connratelimit01'
down_revision: Union[str, Sequence[str], None] = 'm1e2r3g4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default false so existing rows backfill to "disabled".
    op.add_column(
        'connections',
        sa.Column('rate_limit_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'connections',
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=True),
    )
    op.add_column(
        'connections',
        sa.Column('rate_limit_per_hour', sa.Integer(), nullable=True),
    )
    op.add_column(
        'connections',
        sa.Column('rate_limit_per_day', sa.Integer(), nullable=True),
    )

    op.create_table(
        'connection_rate_limit_counters',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('window', sa.String(length=16), nullable=False),
        sa.Column('bucket_start', sa.DateTime(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'connection_id', 'window', 'bucket_start',
            name='uq_conn_rate_limit_window_bucket',
        ),
    )
    op.create_index(
        'ix_conn_rate_limit_lookup',
        'connection_rate_limit_counters',
        ['connection_id', 'window', 'bucket_start'],
    )
    op.create_index(
        op.f('ix_connection_rate_limit_counters_id'),
        'connection_rate_limit_counters',
        ['id'],
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_connection_rate_limit_counters_id'), table_name='connection_rate_limit_counters')
    op.drop_index('ix_conn_rate_limit_lookup', table_name='connection_rate_limit_counters')
    op.drop_table('connection_rate_limit_counters')
    op.drop_column('connections', 'rate_limit_per_day')
    op.drop_column('connections', 'rate_limit_per_hour')
    op.drop_column('connections', 'rate_limit_per_minute')
    op.drop_column('connections', 'rate_limit_enabled')
