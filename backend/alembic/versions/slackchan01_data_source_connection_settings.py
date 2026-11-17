"""add data_source_connection_settings (per-agent Slack channel scope)

Revision ID: slackchan01
Revises: queryexec03
Create Date: 2026-11-17 18:00:00.000000

Per-agent settings for a shared connection. First use: which Slack channels
(by stable channel id) an agent reads through a slack_messages connection, and
whether it also reads the user's DMs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'slackchan01'
down_revision: Union[str, Sequence[str], None] = 'queryexec03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'data_source_connection_settings',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('data_source_id', sa.String(36), sa.ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('connection_id', sa.String(36), sa.ForeignKey('connections.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('data_source_id', 'connection_id', name='uq_dscs_ds_conn'),
    )


def downgrade() -> None:
    op.drop_table('data_source_connection_settings')
