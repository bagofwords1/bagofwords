"""add external_platform_channels (discover-on-traffic channel registry)

Revision ID: extplatchanlist01
Revises: extplatchan01
Create Date: 2026-11-10 00:10:00.000000

Records channels seen on an external-platform instance so the settings UI can
offer a channel -> agent picker without any extra OAuth scopes. Purely
additive: no rows means no discovered channels yet (the admin can still add one
by id), and the table never affects routing — the scope lives in
``external_platform_agents``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'extplatchanlist01'
down_revision: Union[str, None] = 'extplatchan01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'external_platform_channels',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('platform_id', sa.String(length=36), nullable=False),
        sa.Column('external_channel_id', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('channel_type', sa.String(length=64), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['platform_id'], ['external_platforms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform_id', 'external_channel_id', name='uq_external_platform_channel'),
    )
    op.create_index(
        'ix_external_platform_channels_organization_id',
        'external_platform_channels', ['organization_id'],
    )
    op.create_index(
        'ix_external_platform_channels_platform_id',
        'external_platform_channels', ['platform_id'],
    )
    op.create_index(
        'ix_external_platform_channels_external_channel_id',
        'external_platform_channels', ['external_channel_id'],
    )
    op.create_index(
        'ix_external_platform_channels_id', 'external_platform_channels', ['id'],
    )


def downgrade() -> None:
    op.drop_index('ix_external_platform_channels_id', table_name='external_platform_channels')
    op.drop_index('ix_external_platform_channels_external_channel_id', table_name='external_platform_channels')
    op.drop_index('ix_external_platform_channels_platform_id', table_name='external_platform_channels')
    op.drop_index('ix_external_platform_channels_organization_id', table_name='external_platform_channels')
    op.drop_table('external_platform_channels')
