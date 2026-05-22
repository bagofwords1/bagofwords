"""add data_source_connection_tool per-agent tool overlay

Revision ID: e9f0a1b2c3d4
Revises: c5d6e7f8g9h0, d6e7f8a9b0c1
Create Date: 2026-05-22 12:00:00.000000

Merges the two pre-existing heads (PR 275 ``uq_connections_org_name`` and
``add_events_json``) and adds the per-agent tool overlay table used by the
agent YAML apply flow and the ``/data_sources/{id}/tools`` endpoints.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9f0a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = ('c5d6e7f8g9h0', 'd6e7f8a9b0c1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'data_source_connection_tool',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('data_source_id', sa.String(36), sa.ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('connection_tool_id', sa.String(36), sa.ForeignKey('connection_tools.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('policy', sa.String(), nullable=False, server_default='allow'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('data_source_id', 'connection_tool_id', name='uq_dsct_ds_tool'),
    )


def downgrade() -> None:
    op.drop_table('data_source_connection_tool')
