"""add auto-reindex columns to connections

Revision ID: g0a1b2c3d4e5
Revises: f9a0b1c2d3e4
Create Date: 2026-05-24 16:00:00.000000

Adds per-connection auto-reindex settings: an on/off toggle (default on) and
an interval in hours (default 24). A background scheduler job scans for
connections whose last_synced_at is older than the interval and kicks off
an indexing run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g0a1b2c3d4e5'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'auto_reindex_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ))
        batch_op.add_column(sa.Column(
            'auto_reindex_interval_hours',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('24'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_column('auto_reindex_interval_hours')
        batch_op.drop_column('auto_reindex_enabled')
