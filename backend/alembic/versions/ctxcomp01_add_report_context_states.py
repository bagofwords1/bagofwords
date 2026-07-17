"""Add report_context_states for rolling context compaction

Revision ID: ctxcomp01
Revises: v1s2v3o4t5g6
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ctxcomp01'
down_revision: Union[str, None] = 'v1s2v3o4t5g6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'report_context_states',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('report_id', sa.String(length=36), nullable=False),
        sa.Column('summary_json', sa.JSON(), nullable=False),
        sa.Column('covers_until_completion_id', sa.String(length=36), nullable=True),
        sa.Column('covered_turns', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_compacted_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_compaction_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id']),
        sa.ForeignKeyConstraint(['covers_until_completion_id'], ['completions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_report_context_states_id'), 'report_context_states', ['id'], unique=True)
    op.create_index(op.f('ix_report_context_states_report_id'), 'report_context_states', ['report_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_report_context_states_report_id'), table_name='report_context_states')
    op.drop_index(op.f('ix_report_context_states_id'), table_name='report_context_states')
    op.drop_table('report_context_states')
