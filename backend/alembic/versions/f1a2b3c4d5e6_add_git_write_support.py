"""add git write support

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2025-01-01 10:00:00.000000

Adds PAT support and write capabilities for bidirectional Git sync.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns to git_repositories table
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        # Self-hosted support
        batch_op.add_column(sa.Column('custom_host', sa.String(length=255), nullable=True))
        
        # PAT for HTTPS + API operations
        batch_op.add_column(sa.Column('access_token', sa.Text(), nullable=True))
        
        # Username for Bitbucket Cloud (requires username:app_password)
        batch_op.add_column(sa.Column('access_token_username', sa.String(length=255), nullable=True))
        
        # Enable write-back functionality
        batch_op.add_column(sa.Column('write_enabled', sa.Boolean(), nullable=False, server_default='false'))
    
    # Add new columns to instruction_builds table for Git push tracking
    with op.batch_alter_table('instruction_builds', schema=None) as batch_op:
        # Branch name when pushing to Git (e.g., "BOW-42")
        batch_op.add_column(sa.Column('git_branch_name', sa.String(length=255), nullable=True))
        
        # PR URL if created via API
        batch_op.add_column(sa.Column('git_pr_url', sa.String(length=512), nullable=True))
        
        # Timestamp when pushed to Git
        batch_op.add_column(sa.Column('git_pushed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Remove columns from instruction_builds
    with op.batch_alter_table('instruction_builds', schema=None) as batch_op:
        batch_op.drop_column('git_pushed_at')
        batch_op.drop_column('git_pr_url')
        batch_op.drop_column('git_branch_name')
    
    # Remove columns from git_repositories
    with op.batch_alter_table('git_repositories', schema=None) as batch_op:
        batch_op.drop_column('write_enabled')
        batch_op.drop_column('access_token_username')
        batch_op.drop_column('access_token')
        batch_op.drop_column('custom_host')

