"""add column_stats (per-column usage of agent tables)

Revision ID: colstats01
Revises: rel0631merge
Create Date: 2026-11-22 13:00:00.000000

BOW-215: wide tables (thousands of columns) put every column into the
planner's context. column_stats is the column-level twin of table_stats: it
counts how often each column is used by executed queries (success and
failure) and the thumbs up/down on answers that used it, so the schema
context can rank columns and cap each table to a column budget.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'colstats01'
down_revision: Union[str, Sequence[str], None] = 'rel0631merge'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'column_stats' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'column_stats',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('org_id', sa.String(length=36), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), nullable=False),
        sa.Column('datasource_table_id', sa.String(length=36), nullable=False),
        sa.Column('column_name', sa.String(length=1024), nullable=False),
        sa.Column('usage_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('success_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('failure_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('weighted_usage_count', sa.Float(), nullable=False, server_default='0'),
        sa.Column('pos_feedback_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('neg_feedback_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('weighted_pos_feedback', sa.Float(), nullable=False, server_default='0'),
        sa.Column('weighted_neg_feedback', sa.Float(), nullable=False, server_default='0'),
        sa.Column('rank_weight', sa.Float(), nullable=False, server_default='0'),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_feedback_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at_stats', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['datasource_table_id'], ['datasource_tables.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('datasource_table_id', 'column_name', name='uq_colstats_table_column'),
    )
    op.create_index('ix_column_stats_id', 'column_stats', ['id'], unique=True)
    op.create_index('ix_colstats_ds', 'column_stats', ['data_source_id'])
    op.create_index('ix_colstats_table_rank', 'column_stats', ['datasource_table_id', 'rank_weight'])


def downgrade() -> None:
    op.drop_index('ix_colstats_table_rank', table_name='column_stats')
    op.drop_index('ix_colstats_ds', table_name='column_stats')
    op.drop_index('ix_column_stats_id', table_name='column_stats')
    op.drop_table('column_stats')
