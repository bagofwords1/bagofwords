"""instruction file architecture

Revision ID: d4e5f6a7b8c9
Revises: c3d8f5a2b1e7
Create Date: 2025-12-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d8f5a2b1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new fields to instructions table for 1 file = 1 instruction model
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_file_path', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))
    
    # Add last indexed commit tracking to git_repositories
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_indexed_commit_sha', sa.String(length=40), nullable=True))
    
    # Add chunk position fields to metadata_resources
    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chunk_start_line', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('chunk_end_line', sa.Integer(), nullable=True))
    
    # Add file-level progress tracking to metadata_indexing_jobs
    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('total_files', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('processed_files', sa.Integer(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('current_phase', sa.String(length=50), nullable=True))


def downgrade() -> None:
    # Remove file-level progress tracking from metadata_indexing_jobs
    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.drop_column('current_phase')
        batch_op.drop_column('processed_files')
        batch_op.drop_column('total_files')
    
    # Remove chunk position fields from metadata_resources
    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.drop_column('chunk_end_line')
        batch_op.drop_column('chunk_start_line')
    
    # Remove last indexed commit tracking from git_repositories
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.drop_column('last_indexed_commit_sha')
    
    # Remove new fields from instructions table
    with op.batch_alter_table('instructions', schema=None) as batch_op:
        batch_op.drop_column('content_hash')
        batch_op.drop_column('source_file_path')
