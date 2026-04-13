"""add report sharing visibility columns and report_shares table

Revision ID: e7f8g9h0i1j2
Revises: d5e6f7g8h9i0
Create Date: 2026-04-13 00:00:00.000000

Adds artifact_visibility and conversation_visibility columns to reports,
and creates the report_shares table for per-user sharing grants.

Data migration: existing published reports get artifact_visibility='public'.
Existing reports with conversation_share_enabled=true get conversation_visibility='public'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8g9h0i1j2'
down_revision: Union[str, None] = 'd5e6f7g8h9i0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add visibility columns to reports
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('artifact_visibility', sa.String(), nullable=False, server_default='none')
        )
        batch_op.add_column(
            sa.Column('conversation_visibility', sa.String(), nullable=False, server_default='none')
        )

    # Data migration: sync visibility from existing status / conversation_share_enabled
    op.execute(
        "UPDATE reports SET artifact_visibility = 'public' WHERE status = 'published'"
    )
    op.execute(
        "UPDATE reports SET conversation_visibility = 'public' WHERE conversation_share_enabled = true"
    )

    # Create report_shares table
    op.create_table(
        'report_shares',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('share_type', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_report_shares_report_id', 'report_shares', ['report_id'])
    op.create_index('ix_report_shares_user_id', 'report_shares', ['user_id'])
    op.create_index('ix_report_shares_share_type', 'report_shares', ['share_type'])
    op.create_unique_constraint('uq_report_share', 'report_shares', ['report_id', 'user_id', 'share_type'])


def downgrade() -> None:
    op.drop_constraint('uq_report_share', 'report_shares', type_='unique')
    op.drop_index('ix_report_shares_share_type', table_name='report_shares')
    op.drop_index('ix_report_shares_user_id', table_name='report_shares')
    op.drop_index('ix_report_shares_report_id', table_name='report_shares')
    op.drop_table('report_shares')

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('conversation_visibility')
        batch_op.drop_column('artifact_visibility')
