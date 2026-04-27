"""add cache_read_tokens and cache_creation_tokens to llm_usage_records

Revision ID: f2a3b4c5d6e7
Revises: e1f2d3c4b5a6
Create Date: 2026-04-26 00:00:00.000000

Stores per-call cache token counts alongside prompt/completion tokens so the
console can show accurate token volume and cost breakdown for Anthropic prompt
caching and OpenAI automatic prefix caching.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2d3c4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_usage_records', sa.Column('cache_read_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('llm_usage_records', sa.Column('cache_creation_tokens', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('llm_usage_records', 'cache_creation_tokens')
    op.drop_column('llm_usage_records', 'cache_read_tokens')
