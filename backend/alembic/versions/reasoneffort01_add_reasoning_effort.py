"""add reasoning_effort beside model_id on reports, webhooks and prompts

Revision ID: reasoneffort01
Revises: artchatmodel01
Create Date: 2026-09-28 00:00:00.000000

The reasoning level a user picks together with a model (Low / Medium / High /
Max). Stored wherever a model override is stored; null = Default. Scheduled
prompts and eval test cases keep it inside their existing prompt JSON.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'reasoneffort01'
down_revision: Union[str, None] = 'artchatmodel01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('reports', 'webhooks', 'prompts')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('reasoning_effort', sa.String(length=16), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('reasoning_effort')
