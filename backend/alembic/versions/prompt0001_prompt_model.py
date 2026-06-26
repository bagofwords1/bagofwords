"""prompt model: completion-shaped, multi-agent, scoped

Extends the existing `prompts` table into a reusable, access-scoped prompt
(mode/model/mentions/parameters/scope/is_starter) and adds a prompt<->data_source
many-to-many. No scheduling/subscription/channel columns — just the model.

Revision ID: prompt0001
Revises: c3f1a9b2d4e7
Create Date: 2026-06-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'prompt0001'
down_revision: Union[str, None] = 'c3f1a9b2d4e7'
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

    cols = [
        sa.Column('mode', sa.String(), nullable=False, server_default='chat'),
        sa.Column('model_id', sa.String(length=36), nullable=True),
        sa.Column('mentions', sa.JSON(), nullable=True),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('scope', sa.String(), nullable=False, server_default='agent'),
        sa.Column('is_starter', sa.Boolean(), nullable=False, server_default=sa.false()),
    ]
    for col in cols:
        if not _has_column(insp, 'prompts', col.name):
            op.add_column('prompts', col)

    if 'prompt_data_source_association' not in insp.get_table_names():
        op.create_table(
            'prompt_data_source_association',
            sa.Column('prompt_id', sa.String(length=36), sa.ForeignKey('prompts.id'), nullable=True),
            sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id'), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'prompt_data_source_association' in insp.get_table_names():
        op.drop_table('prompt_data_source_association')
    for col in ['mode', 'model_id', 'mentions', 'parameters', 'scope', 'is_starter']:
        if _has_column(insp, 'prompts', col):
            op.drop_column('prompts', col)
