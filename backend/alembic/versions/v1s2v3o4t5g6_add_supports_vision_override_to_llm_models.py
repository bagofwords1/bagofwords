"""add supports_vision_override to llm_models

Revision ID: v1s2v3o4t5g6
Revises: dsicon0001
Create Date: 2026-07-12 12:00:00.000000

Adds a nullable supports_vision_override column to llm_models so admins can
manually toggle image support per model. NULL means "follow the catalog"
(LLM_MODEL_DETAILS); True/False is an explicit override that survives catalog
re-syncs. The existing supports_vision column stays the resolved value read at
inference time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1s2v3o4t5g6'
down_revision: Union[str, None] = 'dsicon0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supports_vision_override', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('supports_vision_override')
