"""add review_items (admin Review feed)

Revision ID: rev1ew0001
Revises: 4607665ae190
Create Date: 2026-06-17 09:00:00.000000

Adds ``review_items`` — the admin Review feed: actionable items about an agent
(instruction suggestions, schema changes, slow queries, low-confidence runs)
that an admin can triage and resolve (run eval / run training / accept / dismiss).

Topic = an agent (data_source_id) or global (null). State is shared. Dedup is by
(organization_id, data_source_id, type, group_key) for active items.

Stored as plain JSON/String for clean SQLite/Postgres parity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'rev1ew0001'
down_revision: Union[str, None] = '4607665ae190'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'review_items',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), nullable=True),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('severity', sa.String(), nullable=False, server_default='info'),
        sa.Column('status', sa.String(), nullable=False, server_default='open'),
        sa.Column('disposition', sa.String(), nullable=False, server_default='notify'),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('why', sa.String(), nullable=True),
        sa.Column('subject_json', sa.JSON(), nullable=True),
        sa.Column('group_key', sa.String(), nullable=True),
        sa.Column('group_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('resolution_json', sa.JSON(), nullable=True),
        sa.Column('spawned_json', sa.JSON(), nullable=True),
        sa.Column('source_run_id', sa.String(length=36), nullable=True),
        sa.Column('build_id', sa.String(length=36), nullable=True),
        sa.Column('caused_by_id', sa.String(length=36), nullable=True),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('read_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('resolved_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('dismissed_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('snoozed_until', sa.DateTime(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['read_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['resolved_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['dismissed_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_review_items_organization_id', 'review_items', ['organization_id'])
    op.create_index('ix_review_items_data_source_id', 'review_items', ['data_source_id'])
    op.create_index('ix_review_items_type', 'review_items', ['type'])
    op.create_index('ix_review_items_status', 'review_items', ['status'])
    op.create_index('ix_review_items_group_key', 'review_items', ['group_key'])
    op.create_index('ix_review_items_org_status_type', 'review_items', ['organization_id', 'status', 'type'])
    op.create_index('ix_review_items_dedup', 'review_items', ['organization_id', 'data_source_id', 'type', 'group_key'])


def downgrade() -> None:
    op.drop_index('ix_review_items_dedup', table_name='review_items')
    op.drop_index('ix_review_items_org_status_type', table_name='review_items')
    op.drop_index('ix_review_items_group_key', table_name='review_items')
    op.drop_index('ix_review_items_status', table_name='review_items')
    op.drop_index('ix_review_items_type', table_name='review_items')
    op.drop_index('ix_review_items_data_source_id', table_name='review_items')
    op.drop_index('ix_review_items_organization_id', table_name='review_items')
    op.drop_table('review_items')
