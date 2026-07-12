"""add user_connection_tool_preferences per-user tool policy

Revision ID: uctp0001
Revises: dsicon0001
Create Date: 2026-07-12 12:00:00.000000

Per-user policy preference (allow | ask | deny | auto) for a connection tool.
Sits above the admin layers (ConnectionTool default + DataSourceConnectionTool
per-agent overlay); an admin 'deny' remains absolute at resolution time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'uctp0001'
down_revision: Union[str, Sequence[str], None] = 'notes0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_connection_tool_preferences',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('connection_tool_id', sa.String(36), sa.ForeignKey('connection_tools.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('policy', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'connection_tool_id', name='uq_uctp_user_tool'),
    )


def downgrade() -> None:
    op.drop_table('user_connection_tool_preferences')
