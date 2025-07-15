"""add external platform to report

Revision ID: c76b76a7869a
Revises: 2ef912b5a237
Create Date: 2025-07-15 10:25:31.006677

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c76b76a7869a'
down_revision: Union[str, None] = '2ef912b5a237'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_platform_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_reports_external_platform_id', 'external_platforms',
            ['external_platform_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_constraint('fk_reports_external_platform_id', type_='foreignkey')
        batch_op.drop_column('external_platform_id')
