"""add unified instruction fields

Revision ID: b8f3a2c91d4e
Revises: a295ec7c4a5b
Create Date: 2025-12-17 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8f3a2c91d4e'
down_revision: Union[str, None] = 'a295ec7c4a5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new fields to git_repositories table for instruction sync settings
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_publish', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('default_load_mode', sa.String(length=20), nullable=True))
    
    # Set default values for git_repositories (use false/true for PostgreSQL compatibility)
    op.execute("UPDATE git_repositories SET auto_publish = false WHERE auto_publish IS NULL")
    op.execute("UPDATE git_repositories SET default_load_mode = 'auto' WHERE default_load_mode IS NULL")
    
    # Make columns non-nullable after setting defaults
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.alter_column('auto_publish', existing_type=sa.Boolean(), nullable=False)
        batch_op.alter_column('default_load_mode', existing_type=sa.String(length=20), nullable=False)
    
    # Add new fields to instructions table
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        # Source tracking
        batch_op.add_column(sa.Column('source_type', sa.String(length=20), nullable=True))
        
        # Git source info
        batch_op.add_column(sa.Column('source_metadata_resource_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('source_git_commit_sha', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('source_sync_enabled', sa.Boolean(), nullable=True))
        
        # Loading behavior
        batch_op.add_column(sa.Column('load_mode', sa.String(length=20), nullable=True))
        
        # Display
        batch_op.add_column(sa.Column('title', sa.String(length=255), nullable=True))
        
        # Structured data
        batch_op.add_column(sa.Column('structured_data', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('formatted_content', sa.Text(), nullable=True))
        
        # Make user_id nullable (git-sourced instructions don't have a user creator)
        batch_op.alter_column('user_id', existing_type=sa.String(length=36), nullable=True)
        
        # Foreign key for source_metadata_resource_id with ON DELETE SET NULL
        # This allows metadata_resources to be deleted without breaking instructions
        batch_op.create_foreign_key(
            'fk_instruction_source_metadata_resource', 
            'metadata_resources', 
            ['source_metadata_resource_id'], 
            ['id'],
            ondelete='SET NULL'
        )
    
    # Add new fields to metadata_resources table
    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('load_mode', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('instruction_id', sa.String(length=36), nullable=True))
        
        # Foreign key for instruction_id with ON DELETE SET NULL
        # This allows instructions to be deleted without breaking metadata_resources
        batch_op.create_foreign_key(
            'fk_metadata_resource_instruction',
            'instructions',
            ['instruction_id'],
            ['id'],
            ondelete='SET NULL'
        )
    
    # Populate default values for existing data
    # Set source_type based on existing ai_source field
    op.execute("UPDATE instructions SET source_type = 'ai' WHERE ai_source IS NOT NULL")
    op.execute("UPDATE instructions SET source_type = 'user' WHERE source_type IS NULL")
    
    # Set default load_mode for existing instructions
    op.execute("UPDATE instructions SET load_mode = 'always' WHERE load_mode IS NULL")
    
    # Set default load_mode for existing metadata_resources
    op.execute("UPDATE metadata_resources SET load_mode = 'intelligent' WHERE load_mode IS NULL")
    
    # Set default source_sync_enabled (use true for PostgreSQL compatibility)
    op.execute("UPDATE instructions SET source_sync_enabled = true WHERE source_sync_enabled IS NULL")


def downgrade() -> None:
    # Remove fields from git_repositories table
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.drop_column('default_load_mode')
        batch_op.drop_column('auto_publish')
    
    # Remove fields from metadata_resources table
    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.drop_constraint('fk_metadata_resource_instruction', type_='foreignkey')
        batch_op.drop_column('instruction_id')
        batch_op.drop_column('load_mode')
    
    # Remove fields from instructions table
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_instruction_source_metadata_resource', type_='foreignkey')
        batch_op.drop_column('formatted_content')
        batch_op.drop_column('structured_data')
        batch_op.drop_column('title')
        batch_op.drop_column('load_mode')
        batch_op.drop_column('source_sync_enabled')
        batch_op.drop_column('source_git_commit_sha')
        batch_op.drop_column('source_metadata_resource_id')
        batch_op.drop_column('source_type')
        # Note: Not reverting user_id to NOT NULL as it would fail if git instructions exist

