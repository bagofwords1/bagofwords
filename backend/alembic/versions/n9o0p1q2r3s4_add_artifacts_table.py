"""add artifacts table

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2025-01-23 10:00:00.000000

Creates the artifacts table for storing AI-generated React dashboard code.
Supports both 'page' and 'slides' modes with flexible JSON content.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'n9o0p1q2r3s4'
down_revision: Union[str, None] = 'm8n9o0p1q2r3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),

        # Foreign keys
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False, index=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False, index=True),
        sa.Column('completion_id', sa.String(36), sa.ForeignKey('completions.id'), nullable=True, index=True),

        # Artifact metadata
        sa.Column('title', sa.String(255), nullable=True),
        sa.Column('mode', sa.String(20), nullable=False, default='page', index=True),
        sa.Column('version', sa.Integer(), nullable=False, default=1),

        # Content storage (JSON)
        sa.Column('content', sa.JSON(), nullable=False),

        # Generation tracking
        sa.Column('generation_prompt', sa.Text(), nullable=True),

        # Status: 'pending', 'completed', 'failed'
        sa.Column('status', sa.String(20), nullable=False, default='completed', index=True),
    )

    # Add index for common queries
    op.create_index('ix_artifacts_report_created', 'artifacts', ['report_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_artifacts_report_created', table_name='artifacts')
    op.drop_table('artifacts')
