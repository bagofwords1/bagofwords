"""add ai judgement and step status reason

Revision ID: 107fc9d42143
Revises: 34420bdfc530
Create Date: 2025-07-30 09:40:30.826329

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '107fc9d42143'
down_revision: Union[str, None] = '34420bdfc530'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('instructions_effectiveness', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('context_effectiveness', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('response_score', sa.Integer(), nullable=True))

    with op.batch_alter_table('steps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status_reason', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('steps', schema=None) as batch_op:
        batch_op.drop_column('status_reason')

    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.drop_column('response_score')
        batch_op.drop_column('context_effectiveness')
        batch_op.drop_column('instructions_effectiveness')