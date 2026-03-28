"""add connection tools tables for MCP and custom API support

Revision ID: a1b2c3d4e5f6
Revises: z0a1b2c3d4e5
Create Date: 2026-03-28 00:00:00.000000

Adds connection_tools and user_connection_tools tables to support
MCP servers and custom API endpoints as tool providers on connections.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'z0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'connection_tools',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('connection_id', sa.String(36), sa.ForeignKey('connections.id'), nullable=False, index=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('input_schema', sa.JSON(), nullable=True),
        sa.Column('output_schema', sa.JSON(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('policy', sa.String(), nullable=False, server_default='allow'),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('connection_id', 'name', name='uq_connection_tool_name'),
    )

    op.create_table(
        'user_connection_tools',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('connection_id', sa.String(36), sa.ForeignKey('connections.id'), nullable=False, index=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('tool_name', sa.String(), nullable=False),
        sa.Column('connection_tool_id', sa.String(36), sa.ForeignKey('connection_tools.id'), nullable=True),
        sa.Column('is_accessible', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(), nullable=False, server_default='accessible'),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('connection_id', 'user_id', 'tool_name', name='uq_user_conn_tool'),
    )


def downgrade() -> None:
    op.drop_table('user_connection_tools')
    op.drop_table('connection_tools')
