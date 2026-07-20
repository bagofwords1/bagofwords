"""add supports_image_generation_override to llm_models

Revision ID: img2gen3ovr4
Revises: img1gen2col3
Create Date: 2026-07-20 00:00:00.000000

Admin override for image generation (mark/unmark a model as an image model),
NULL = follow the catalog. Mirrors supports_vision_override; persists the toggle
across catalog re-syncs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'img2gen3ovr4'
down_revision: Union[str, None] = 'img1gen2col3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supports_image_generation_override', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('supports_image_generation_override')
