"""add memberships.checkins_opt_out

Revision ID: agentcheckins02
Revises: agentcheckins01
Create Date: 2026-09-26 00:00:01.000000

Per-user opt-out of agent check-ins, respected while the org setting is on.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'agentcheckins02'
down_revision: Union[str, None] = 'agentcheckins01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('memberships', sa.Column('checkins_opt_out', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('memberships') as batch_op:
        batch_op.drop_column('checkins_opt_out')
