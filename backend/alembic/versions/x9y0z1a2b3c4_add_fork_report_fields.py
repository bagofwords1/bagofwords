"""add fork report fields

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-03-22 00:00:00.000000

Adds forked_from_id to reports and fork summary fields to completions
to support the fork report feature.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'x9y0z1a2b3c4'
down_revision: Union[str, None] = 'w8x9y0z1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('forked_from_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=True))
        batch_op.create_index('ix_reports_forked_from_id', ['forked_from_id'])

    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_fork_summary', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('source_report_id', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('fork_asset_refs', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.drop_column('fork_asset_refs')
        batch_op.drop_column('source_report_id')
        batch_op.drop_column('is_fork_summary')

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_index('ix_reports_forked_from_id')
        batch_op.drop_column('forked_from_id')
