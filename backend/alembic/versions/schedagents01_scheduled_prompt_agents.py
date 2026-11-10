"""scheduled task agents: per-task agent selection

Revision ID: schedagents01
Revises: rel0611
Create Date: 2026-11-08 19:00:00.000000

A scheduled task can name the agents its runs use. NULL keeps the old
behavior: the run uses the host report's agents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'schedagents01'
down_revision: Union[str, None] = 'rel0611'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return any(c["name"] == "data_source_ids" for c in sa.inspect(op.get_bind()).get_columns("scheduled_prompts"))


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_column():
        return
    with op.batch_alter_table('scheduled_prompts') as batch_op:
        batch_op.add_column(sa.Column('data_source_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table('scheduled_prompts') as batch_op:
        batch_op.drop_column('data_source_ids')
