"""add report_stars table for per-user report starring

Revision ID: f3a4b5c6d7e8
Revises: overlay_fk_ondelete
Create Date: 2026-06-06 00:00:00.000000

Creates the report_stars table. Each row marks that a specific user has
starred (favorited) a report. Starring is per-user, so the same report can
be starred independently by different users. Starred reports are surfaced at
the top of the reports list.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'overlay_fk_ondelete'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'report_stars',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('report_id', 'user_id', name='uq_report_star'),
    )
    op.create_index('ix_report_stars_report_id', 'report_stars', ['report_id'])
    op.create_index('ix_report_stars_user_id', 'report_stars', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_report_stars_user_id', table_name='report_stars')
    op.drop_index('ix_report_stars_report_id', table_name='report_stars')
    op.drop_table('report_stars')
