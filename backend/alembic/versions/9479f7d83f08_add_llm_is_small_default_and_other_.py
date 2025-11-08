"""add llm is_small_default and other params like context size

Revision ID: 9479f7d83f08
Revises: 95de3b2ebc88
Create Date: 2025-11-08 18:47:06.477926

"""
from typing import Sequence, Union
from sqlalchemy import false

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9479f7d83f08'
down_revision: Union[str, None] = '95de3b2ebc88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_small_default', sa.Boolean(), nullable=False, server_default=false()))
        batch_op.add_column(sa.Column('context_window_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('max_output_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('input_cost_per_million_tokens_usd', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('output_cost_per_million_tokens_usd', sa.Float(), nullable=True))


def downgrade() -> None:

    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('output_cost_per_million_tokens_usd')
        batch_op.drop_column('input_cost_per_million_tokens_usd')
        batch_op.drop_column('max_output_tokens')
        batch_op.drop_column('context_window_tokens')
        batch_op.drop_column('is_small_default')