"""add sub_timings_json to tool_executions and duration_ms to completion_blocks

Revision ID: z1a2b3c4d5e6
Revises: y0z1a2b3c4d5
Create Date: 2026-03-29 00:00:00.000000

Adds per-query timing and block-level duration to support bottleneck diagnosis
(e.g. flaky DB/proxy). sub_timings_json stores codegen_ms, execution_ms, and
per-query breakdown. duration_ms on completion_blocks mirrors tool_execution
duration for quick frontend access without joining tool_executions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'z1a2b3c4d5e6'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tool_executions', sa.Column('sub_timings_json', sa.JSON(), nullable=True))
    op.add_column('completion_blocks', sa.Column('duration_ms', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('completion_blocks', 'duration_ms')
    op.drop_column('tool_executions', 'sub_timings_json')
