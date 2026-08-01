"""allow sharing reports with groups

Revision ID: rsgrp0001
Revises: fccfb9232670
Create Date: 2026-07-30 00:00:00.000000

Adds group_id to report_shares so a report's artifact/conversation can be
shared with a whole group, not just individual members. user_id becomes
nullable — each row now names exactly one principal (user OR group).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'rsgrp0001'
down_revision: Union[str, None] = 'fccfb9232670'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('report_shares', schema=None) as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.String(36), nullable=True)
        batch_op.add_column(sa.Column('group_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            'fk_report_shares_group_id', 'groups', ['group_id'], ['id']
        )
        batch_op.create_unique_constraint(
            'uq_report_share_group', ['report_id', 'group_id', 'share_type']
        )
    op.create_index('ix_report_shares_group_id', 'report_shares', ['group_id'])


def downgrade() -> None:
    # Group share rows cannot survive a non-null user_id; drop them first.
    op.execute("DELETE FROM report_shares WHERE user_id IS NULL")
    op.drop_index('ix_report_shares_group_id', table_name='report_shares')
    with op.batch_alter_table('report_shares', schema=None) as batch_op:
        batch_op.drop_constraint('uq_report_share_group', type_='unique')
        batch_op.drop_constraint('fk_report_shares_group_id', type_='foreignkey')
        batch_op.drop_column('group_id')
        batch_op.alter_column('user_id', existing_type=sa.String(36), nullable=False)
