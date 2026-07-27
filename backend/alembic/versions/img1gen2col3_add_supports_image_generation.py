"""add supports_image_generation to llm_models

Revision ID: img1gen2col3
Revises: mrgheads02
Create Date: 2026-07-19 00:00:00.000000

Adds supports_image_generation column to llm_models: whether a model *produces*
images (image-generation models like gpt-image-1), distinct from supports_vision
(accepts image inputs). Gates LLM.generate_image and the generate_image tool.
"""
from typing import Sequence, Union
from sqlalchemy import false

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'img1gen2col3'
down_revision: Union[str, None] = 'mrgheads02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('supports_image_generation', sa.Boolean(), nullable=False, server_default=false())
        )


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('supports_image_generation')
