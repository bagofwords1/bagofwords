"""add parameters to prompts

Adds `prompts.parameters` (template parameter spec) to support parameterized
prompts (user fills values at run time), alongside the existing mentions/mode/
model execution spec.

Revision ID: promptsub02
Revises: promptsub01
Create Date: 2026-06-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'promptsub02'
down_revision: Union[str, None] = 'promptsub01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(insp, table: str, column: str) -> bool:
    try:
        return column in {c['name'] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_column(insp, 'prompts', 'parameters'):
        op.add_column('prompts', sa.Column('parameters', sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_column(insp, 'prompts', 'parameters'):
        op.drop_column('prompts', 'parameters')
