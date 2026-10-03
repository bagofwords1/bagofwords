"""add artifact chat default model to reports

Revision ID: artchatmodel01
Revises: pbiidentity01
Create Date: 2026-09-23 00:00:00.000000

Owner-chosen default LLM for chat on the shared artifact page /r/{id}.
null = inherit the report's model_id, then the viewer's/org default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'artchatmodel01'
down_revision: Union[str, None] = 'pbiidentity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('artifact_chat_model_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column('reports', 'artifact_chat_model_id')
