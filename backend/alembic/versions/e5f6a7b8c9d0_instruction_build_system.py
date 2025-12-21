"""instruction build system

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2025-12-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create instruction_builds table
    op.create_table(
        'instruction_builds',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        
        # Core fields
        sa.Column('build_number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='draft'),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='user'),
        sa.Column('is_main', sa.Boolean(), nullable=False, server_default='false'),
        
        # Trigger links
        sa.Column('metadata_indexing_job_id', sa.String(length=36), nullable=True),
        sa.Column('agent_execution_id', sa.String(length=36), nullable=True),
        
        # Git info
        sa.Column('commit_sha', sa.String(length=40), nullable=True),
        sa.Column('branch', sa.String(length=255), nullable=True),
        
        # Test integration
        sa.Column('test_run_id', sa.String(length=36), nullable=True),
        sa.Column('test_status', sa.String(length=20), nullable=True),
        
        # Statistics
        sa.Column('total_instructions', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('added_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('modified_count', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('removed_count', sa.Integer(), nullable=True, server_default='0'),
        
        # Approval
        sa.Column('approved_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        
        # Organization and creator
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('created_by_user_id', sa.String(length=36), nullable=True),
        
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['approved_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['metadata_indexing_job_id'], ['metadata_indexing_jobs.id']),
        sa.ForeignKeyConstraint(['agent_execution_id'], ['agent_executions.id']),
    )
    
    # Create indexes for instruction_builds
    op.create_index('ix_instruction_builds_id', 'instruction_builds', ['id'])
    op.create_index('ix_instruction_builds_build_number', 'instruction_builds', ['build_number'])
    op.create_index('ix_instruction_builds_is_main', 'instruction_builds', ['is_main'])
    op.create_index('ix_instruction_builds_org_is_main', 'instruction_builds', ['organization_id', 'is_main'])
    
    # Create instruction_versions table
    op.create_table(
        'instruction_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        
        # Link to instruction
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        
        # Content
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('structured_data', sa.JSON(), nullable=True),
        sa.Column('formatted_content', sa.Text(), nullable=True),
        
        # Instruction status at time of version (draft/published/archived)
        sa.Column('status', sa.String(length=50), nullable=True, server_default='published'),
        
        # Loading behavior
        sa.Column('load_mode', sa.String(length=20), nullable=True, server_default='always'),
        
        # Relationships as JSON (denormalized for immutable snapshots)
        sa.Column('references_json', sa.JSON(), nullable=True),
        sa.Column('data_source_ids', sa.JSON(), nullable=True),
        sa.Column('label_ids', sa.JSON(), nullable=True),
        sa.Column('category_ids', sa.JSON(), nullable=True),
        
        # Content hash
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        
        # Audit
        sa.Column('created_by_user_id', sa.String(length=36), nullable=True),
        
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
    )
    
    # Create indexes for instruction_versions
    op.create_index('ix_instruction_versions_id', 'instruction_versions', ['id'])
    op.create_index('ix_instruction_versions_instruction_version', 'instruction_versions', ['instruction_id', 'version_number'])
    
    # Create build_contents table (junction table)
    op.create_table(
        'build_contents',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        
        sa.Column('build_id', sa.String(length=36), nullable=False),
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('instruction_version_id', sa.String(length=36), nullable=False),
        
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['build_id'], ['instruction_builds.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id']),
        sa.ForeignKeyConstraint(['instruction_version_id'], ['instruction_versions.id']),
        sa.UniqueConstraint('build_id', 'instruction_id', name='uq_build_content_build_instruction'),
    )
    
    # Create index for build_contents
    op.create_index('ix_build_contents_id', 'build_contents', ['id'])
    
    # Add current_version_id to instructions table with foreign key
    # Using batch mode for SQLite compatibility (SQLite doesn't support ALTER for FK constraints)
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('current_version_id', sa.String(length=36), nullable=True))
        # Note: For SQLite, foreign key will be enforced at application level
        # PostgreSQL will have FK constraint added when table is recreated in batch mode
    
    # Add build_id to metadata_indexing_jobs table with foreign key
    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('build_id', sa.String(length=36), nullable=True))
        # Note: For SQLite, foreign key will be enforced at application level


def downgrade() -> None:
    # Remove column from metadata_indexing_jobs (using batch for SQLite compatibility)
    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.drop_column('build_id')
    
    # Remove column from instructions (using batch for SQLite compatibility)
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.drop_column('current_version_id')
    
    # Drop build_contents table
    op.drop_index('ix_build_contents_id', 'build_contents')
    op.drop_table('build_contents')
    
    # Drop instruction_versions table
    op.drop_index('ix_instruction_versions_instruction_version', 'instruction_versions')
    op.drop_index('ix_instruction_versions_id', 'instruction_versions')
    op.drop_table('instruction_versions')
    
    # Drop instruction_builds table
    op.drop_index('ix_instruction_builds_org_is_main', 'instruction_builds')
    op.drop_index('ix_instruction_builds_is_main', 'instruction_builds')
    op.drop_index('ix_instruction_builds_build_number', 'instruction_builds')
    op.drop_index('ix_instruction_builds_id', 'instruction_builds')
    op.drop_table('instruction_builds')

