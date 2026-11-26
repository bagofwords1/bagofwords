"""add user_connector_preferences — per-user chat on/off for a connector

Revision ID: connchat01
Revises: dailyquota01
Create Date: 2026-11-24 00:00:00.000000

Per-user on/off choice for one personal connector in the chat composer. A row
exists only for an explicit OFF; absence means ON (the connector joins runs as
before). Keyed by (user_id, data_source_id); the workspace is the data source's
organization.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'connchat01'
down_revision: Union[str, Sequence[str], None] = 'dailyquota01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table('user_connector_preferences')


def upgrade() -> None:
    # Idempotent: the dashboards corpus runs schema-only revisions unstamped,
    # so a later `upgrade head` runs this again.
    if _has_table():
        return
    op.create_table(
        'user_connector_preferences',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('data_source_id', sa.String(36), sa.ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'data_source_id', name='uq_ucp_user_ds'),
    )


def downgrade() -> None:
    if not _has_table():
        return
    op.drop_table('user_connector_preferences')
