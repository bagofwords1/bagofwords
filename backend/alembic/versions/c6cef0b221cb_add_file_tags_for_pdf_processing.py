"""add file tags for pdf processing

Revision ID: c6cef0b221cb
Revises: 42a7247575e0
Create Date: 2024-10-19 11:57:58.565770

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column

# revision identifiers, used by Alembic.
revision: str = 'c6cef0b221cb'
down_revision: Union[str, None] = '42a7247575e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('file_tags',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.Column('file_id', get_uuid_column(), nullable=False),
    sa.Column('id', get_uuid_column(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_file_tags_id'), 'file_tags', ['id'], unique=True)
    


def downgrade() -> None:
    op.drop_index(op.f('ix_file_tags_id'), table_name='file_tags')
    op.drop_table('file_tags')

