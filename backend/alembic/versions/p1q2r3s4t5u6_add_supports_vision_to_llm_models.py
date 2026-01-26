"""add supports_vision to llm_models

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2025-01-25 12:00:00.000000

Adds supports_vision column to llm_models table to indicate whether a model accepts image inputs.
"""
from typing import Sequence, Union
from sqlalchemy import false, true

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'p1q2r3s4t5u6'
down_revision: Union[str, None] = 'o0p1q2r3s4t5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supports_vision', sa.Boolean(), nullable=False, server_default=true()))


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('supports_vision')
