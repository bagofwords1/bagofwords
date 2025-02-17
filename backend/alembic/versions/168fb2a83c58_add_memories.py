"""add memories

Revision ID: 168fb2a83c58
Revises: 6ca8f6939a3d
Create Date: 2024-10-07 17:07:35.424183

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column


# revision identifiers, used by Alembic.
revision: str = '168fb2a83c58'
down_revision: Union[str, None] = '6ca8f6939a3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('memories',
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('user_id', get_uuid_column(), nullable=True),
    sa.Column('organization_id', get_uuid_column(), nullable=True),
    sa.Column('step_id', get_uuid_column(), nullable=True),
    sa.Column('report_id', get_uuid_column(), nullable=True),
    sa.Column('is_public', sa.Boolean(), nullable=True),
    sa.Column('id', get_uuid_column(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
    sa.ForeignKeyConstraint(['step_id'], ['steps.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memories_id'), 'memories', ['id'], unique=True)
    


def downgrade() -> None:
    op.drop_index(op.f('ix_memories_id'), table_name='memories')
    op.drop_table('memories')

