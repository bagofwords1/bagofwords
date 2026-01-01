"""add conversation share columns to reports

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2025-01-01 10:00:00.000000

Adds conversation_share_token and conversation_share_enabled columns
to support sharing conversations separately from dashboard publishing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'k6l7m8n9o0p1'
down_revision: Union[str, None] = 'j5k6l7m8n9o0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add conversation share columns to reports table
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conversation_share_token', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('conversation_share_enabled', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.create_index('ix_reports_conversation_share_token', ['conversation_share_token'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_index('ix_reports_conversation_share_token')
        batch_op.drop_column('conversation_share_enabled')
        batch_op.drop_column('conversation_share_token')

