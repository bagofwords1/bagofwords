"""add origin/wake columns to test_runs

Revision ID: evalwake0001
Revises: v1s2v3o4t5g6
Create Date: 2026-07-16 00:00:00.000000

Agent-initiated eval runs (the run_eval tool) now execute in the background
by default. These columns record which conversation started the run so the
background finalizer can wake it with a completion when the run reaches a
terminal status.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'evalwake0001'
down_revision: Union[str, None] = 'v1s2v3o4t5g6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('test_runs', sa.Column('origin_report_id', sa.String(length=36), nullable=True))
    op.add_column('test_runs', sa.Column('origin_user_id', sa.String(length=36), nullable=True))
    # Server default keeps existing rows valid; the ORM default covers new rows.
    op.add_column('test_runs', sa.Column('wake_on_finish', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('ix_test_runs_origin_report_id', 'test_runs', ['origin_report_id'])


def downgrade() -> None:
    op.drop_index('ix_test_runs_origin_report_id', table_name='test_runs')
    op.drop_column('test_runs', 'wake_on_finish')
    op.drop_column('test_runs', 'origin_user_id')
    op.drop_column('test_runs', 'origin_report_id')
