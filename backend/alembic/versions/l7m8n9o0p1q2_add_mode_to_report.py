"""add mode column to reports

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2025-01-05 12:00:00.000000

Adds mode column to reports table to persist the selected mode
(chat, deep, training) across page refreshes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l7m8n9o0p1q2'
down_revision: Union[str, None] = 'k6l7m8n9o0p1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add mode column to reports table with default 'chat'
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mode', sa.String(), nullable=False, server_default='chat'))


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('mode')
