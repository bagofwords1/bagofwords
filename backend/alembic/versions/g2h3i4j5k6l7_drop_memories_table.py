"""drop memories table

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2024-12-24 12:00:00.000000

Remove the memories feature entirely - drops the memories table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column


# revision identifiers, used by Alembic.
revision: str = 'g2h3i4j5k6l7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the memories table
    op.drop_index(op.f('ix_memories_id'), table_name='memories')
    op.drop_table('memories')


def downgrade() -> None:
    # Recreate the memories table
    op.create_table('memories',
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('user_id', get_uuid_column(), nullable=True),
        sa.Column('organization_id', get_uuid_column(), nullable=True),
        sa.Column('step_id', get_uuid_column(), nullable=True),
        sa.Column('report_id', get_uuid_column(), nullable=True),
        sa.Column('widget_id', get_uuid_column(), nullable=True),
        sa.Column('is_public', sa.Boolean(), nullable=True),
        sa.Column('id', get_uuid_column(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
        sa.ForeignKeyConstraint(['step_id'], ['steps.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], name='fk_memories_widget_id'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memories_id'), 'memories', ['id'], unique=True)


