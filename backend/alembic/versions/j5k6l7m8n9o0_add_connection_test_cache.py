"""add connection test cache columns

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2024-12-30 14:00:00.000000

Adds columns to cache connection test results, avoiding repeated slow tests.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j5k6l7m8n9o0'
down_revision: Union[str, None] = 'i4j5k6l7m8n9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add connection test cache columns
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_connection_status', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('last_connection_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_column('last_connection_checked_at')
        batch_op.drop_column('last_connection_status')

