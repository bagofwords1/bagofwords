"""add llm_models.model_kind (chat | decision)

Revision ID: decmodel01
Revises: dashacc01
Create Date: 2026-11-14 00:00:00.000000

Decision models (typed questions in, calibrated probabilities out) are a
separate model kind that must never be offered for chat. A column rather than
a JSON key because every chat-model query filters on it (pickers, defaults,
fallback, routing candidates). Existing rows are chat models. Purely additive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'decmodel01'
down_revision: Union[str, None] = 'dashacc01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    cols = sa.inspect(op.get_bind()).get_columns('llm_models')
    return any(c['name'] == 'model_kind' for c in cols)


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table('llm_models') as batch:
        batch.add_column(sa.Column('model_kind', sa.String(length=16), nullable=False, server_default='chat'))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table('llm_models') as batch:
        batch.drop_column('model_kind')
