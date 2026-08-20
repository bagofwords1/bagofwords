"""add pricing override columns to llm_models

Revision ID: pricovr01
Revises: evalsuitefk01
Create Date: 2026-08-20 12:00:00.000000

Adds nullable input/output_cost_per_million_tokens_usd_override columns to
llm_models so admin-set pricing survives catalog re-syncs. NULL means "follow
the catalog" (LLM_MODEL_DETAILS); a value is an explicit override set through
the pricing endpoint. The existing input/output_cost_per_million_tokens_usd
columns stay the resolved values the cost console and Auto-router read —
previously the models-list catalog sync rewrote them from the catalog on
every load, silently reverting admin corrections.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'pricovr01'
down_revision: Union[str, None] = 'evalsuitefk01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('input_cost_per_million_tokens_usd_override', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('output_cost_per_million_tokens_usd_override', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('output_cost_per_million_tokens_usd_override')
        batch_op.drop_column('input_cost_per_million_tokens_usd_override')
