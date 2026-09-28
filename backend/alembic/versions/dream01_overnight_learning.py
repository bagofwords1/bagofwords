"""overnight learning: dream_runs + watermarks

Revision ID: dream01
Revises: mrgckmem01
Create Date: 2026-09-28 10:00:00.000000

Backs the nightly agent dream (per-agent instruction consolidation) and user
dream (memory upkeep and planned check-ins).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'dream01'
down_revision: Union[str, None] = 'mrgckmem01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_cols():
    return [
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'dream_runs',
        *_base_cols(),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id', ondelete='SET NULL'), nullable=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('local_date', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('status_reason', sa.String(length=64), nullable=True),
        sa.Column('inputs_summary', sa.JSON(), nullable=True),
        sa.Column('tool_calls', sa.JSON(), nullable=True),
        sa.Column('outputs', sa.JSON(), nullable=True),
        sa.Column('tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_dream_runs_id', 'dream_runs', ['id'], unique=True)
    op.create_index('ix_dream_runs_organization_id', 'dream_runs', ['organization_id'])
    op.create_index('ix_dream_runs_status', 'dream_runs', ['status'])
    op.create_index('ix_dream_runs_org_date', 'dream_runs', ['organization_id', 'local_date'])
    op.create_index('ix_dream_runs_user_kind', 'dream_runs', ['user_id', 'kind'])
    op.create_index('ix_dream_runs_ds_kind', 'dream_runs', ['data_source_id', 'kind'])

    op.add_column('memberships', sa.Column('user_dreamed_at', sa.DateTime(), nullable=True))
    op.add_column('data_sources', sa.Column('agent_dreamed_at', sa.DateTime(), nullable=True))
    op.add_column('agent_checkins', sa.Column('origin', sa.String(length=8), nullable=False, server_default='turn'))
    # Plain column (no FK constraint): SQLite can't add a constrained column
    # without a table rebuild, and the link is informational.
    op.add_column('agent_checkins', sa.Column('dream_run_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agent_checkins') as batch_op:
        batch_op.drop_column('dream_run_id')
        batch_op.drop_column('origin')
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.drop_column('agent_dreamed_at')
    with op.batch_alter_table('memberships') as batch_op:
        batch_op.drop_column('user_dreamed_at')
    op.drop_table('dream_runs')
