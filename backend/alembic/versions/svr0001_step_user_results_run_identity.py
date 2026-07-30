"""add step_user_results table and reports.shared_run_identity

Revision ID: svr0001
Revises: v1s2v3o4t5g6
Create Date: 2026-07-16 12:00:00.000000

Per-viewer step result snapshots for shared artifacts: a viewer's "Run"
re-executes the artifact's step code and stores the rows here, keyed by
(step_id, user_id), leaving the shared Step.data snapshot untouched.
reports.shared_run_identity ('viewer' | 'creator') controls whose
data-source credentials viewer-triggered runs use.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'svr0001'
down_revision: Union[str, None] = 'v1s2v3o4t5g6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'step_user_results',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('step_id', sa.String(36), sa.ForeignKey('steps.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False, index=True),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False, index=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='success'),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('executed_as', sa.String(20), nullable=False, server_default='viewer'),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('step_id', 'user_id', name='uq_step_user_results_step_user'),
    )

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('shared_run_identity', sa.String(20), nullable=False, server_default='viewer')
        )


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('shared_run_identity')

    op.drop_table('step_user_results')
