"""add report_views table for per-user last-viewed watermarks

Revision ID: rptview01
Revises: 297905a87c8a
Create Date: 2026-08-01 00:00:00.000000

Creates the report_views table. Each row records when a specific user last
opened a report. A report is "unread" for a user when its last_activity_at
is newer than their watermark (or they have never opened it). Viewing is
per-user, like starring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'rptview01'
down_revision: Union[str, None] = '297905a87c8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'report_views',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('last_viewed_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('report_id', 'user_id', name='uq_report_view'),
    )
    op.create_index('ix_report_views_report_id', 'report_views', ['report_id'])
    op.create_index('ix_report_views_user_id', 'report_views', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_report_views_user_id', table_name='report_views')
    op.drop_index('ix_report_views_report_id', table_name='report_views')
    op.drop_table('report_views')
