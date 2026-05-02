"""add data_source_file_association

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-05-02 00:00:00.000000

Adds the data_source_file_association table so files can be attached
directly to a data source. Files attached at the data source level are
auto-snapshotted into reports created against that data source.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'data_source_file_association',
        sa.Column('data_source_id', sa.String(36), nullable=True),
        sa.Column('file_id', sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id']),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
    )


def downgrade() -> None:
    op.drop_table('data_source_file_association')
