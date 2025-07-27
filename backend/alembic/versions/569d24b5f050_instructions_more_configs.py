"""instructions - more configs

Revision ID: 569d24b5f050
Revises: 45f0bf1a0418
Create Date: 2025-07-26 13:48:37.326333

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '569d24b5f050'
down_revision: Union[str, None] = '45f0bf1a0418'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new status columns as nullable first
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('private_status', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('global_status', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('is_seen', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('can_user_toggle', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_by_user_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('source_instruction_id', sa.String(length=36), nullable=True))
        batch_op.drop_index('ix_instructions_text')
    
    # Update existing rows with default values based on current status
    # All existing instructions become private published
    op.execute("UPDATE instructions SET private_status = 'published' WHERE private_status IS NULL")
    op.execute("UPDATE instructions SET global_status = NULL WHERE global_status IS NULL")
    op.execute("UPDATE instructions SET is_seen = true WHERE is_seen IS NULL")
    op.execute("UPDATE instructions SET can_user_toggle = true WHERE can_user_toggle IS NULL")
    
    # Now make the required columns NOT NULL (except global_status which can be null)
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.alter_column('private_status', nullable=True)  # Can be null for global-only instructions
        batch_op.alter_column('is_seen', nullable=False)
        batch_op.alter_column('can_user_toggle', nullable=False)
        
        # Add foreign key constraints
        batch_op.create_foreign_key('fk_instruction_reviewed_by', 'users', ['reviewed_by_user_id'], ['id'])
        batch_op.create_foreign_key('fk_instruction_source', 'instructions', ['source_instruction_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_instruction_reviewed_by', type_='foreignkey')
        batch_op.drop_constraint('fk_instruction_source', type_='foreignkey')
        batch_op.create_index('ix_instructions_text', ['text'], unique=False)
        batch_op.drop_column('source_instruction_id')
        batch_op.drop_column('reviewed_by_user_id')
        batch_op.drop_column('can_user_toggle')
        batch_op.drop_column('is_seen')
        batch_op.drop_column('global_status')
        batch_op.drop_column('private_status')
