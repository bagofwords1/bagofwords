"""change_instruction_enums_to_strings

Revision ID: 45f0bf1a0418
Revises: b4e1a2ebb2a3
Create Date: 2025-07-20 12:56:59.742305

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45f0bf1a0418'
down_revision: Union[str, None] = 'b4e1a2ebb2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    
    # Drop existing enum columns
    op.drop_column('instructions', 'status')
    op.drop_column('instructions', 'category')
    
    # Add new string columns
    op.add_column('instructions', sa.Column('status', sa.String(length=50), nullable=False, server_default='draft'))
    op.add_column('instructions', sa.Column('category', sa.String(length=50), nullable=False, server_default='general'))
    
    # Drop enum types (PostgreSQL only)
    try:
        op.execute("DROP TYPE IF EXISTS instructionstatus")
        op.execute("DROP TYPE IF EXISTS instructioncategory")
    except Exception:
        # SQLite doesn't have enum types, so this will fail silently
        pass
    
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    
    # Drop string columns
    op.drop_column('instructions', 'category')
    op.drop_column('instructions', 'status')
    
    # Recreate enum types (PostgreSQL only)
    try:
        op.execute("CREATE TYPE instructionstatus AS ENUM ('draft', 'published', 'archived')")
        op.execute("CREATE TYPE instructioncategory AS ENUM ('code_gen', 'data_modeling', 'general')")
        
        # Add back enum columns (PostgreSQL)
        op.add_column('instructions', sa.Column('status', sa.Enum('draft', 'published', 'archived', name='instructionstatus'), nullable=False, server_default='draft'))
        op.add_column('instructions', sa.Column('category', sa.Enum('code_gen', 'data_modeling', 'general', name='instructioncategory'), nullable=False, server_default='general'))
    except Exception:
        # SQLite fallback - use string columns
        op.add_column('instructions', sa.Column('status', sa.String(length=50), nullable=False, server_default='draft'))
        op.add_column('instructions', sa.Column('category', sa.String(length=50), nullable=False, server_default='general'))
    
    # ### end Alembic commands ###