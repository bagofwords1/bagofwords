"""upvotes

Revision ID: b619d8681865
Revises: 6b55dcdbf53f
Create Date: 2025-04-04 17:45:25.211506

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b619d8681865'
down_revision: Union[str, None] = '6b55dcdbf53f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('feedback_score', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('sigkill', sa.DateTime(), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.drop_column('sigkill')
        batch_op.drop_column('feedback_score')