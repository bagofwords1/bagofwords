"""add required_action_json to agent_executions

Revision ID: ra1b2c3d4e5f
Revises: bcbase001
Create Date: 2026-08-05 00:00:00.000000

Records whether a run's required deliverable-producing actions (create_data)
actually succeeded, separately from `status`, which only reflects normal loop
termination. Enables reliability reporting that does not count a run as
successful when every create_data call in it failed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ra1b2c3d4e5f'
down_revision: Union[str, None] = 'bcbase001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_executions', sa.Column('required_action_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_executions', 'required_action_json')
