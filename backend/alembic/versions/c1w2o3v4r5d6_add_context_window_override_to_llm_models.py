"""add context_window_tokens_override to llm_models

Revision ID: c1w2o3v4r5d6
Revises: v1s2v3o4t5g6
Create Date: 2026-07-16 12:00:00.000000

Adds a nullable context_window_tokens_override column to llm_models so admins
can manually size the context window per model (e.g. a Bedrock deployment
capped at 100k). NULL means "follow the catalog" (LLM_MODEL_DETAILS); a value
is an explicit override that survives catalog re-syncs. The existing
context_window_tokens column stays the resolved value the agent's token
budget and prompt-size estimates read.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1w2o3v4r5d6'
down_revision: Union[str, None] = 'v1s2v3o4t5g6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context_window_tokens_override', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('context_window_tokens_override')
