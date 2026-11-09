"""add external_platform_agents (per-instance agent scoping)

Revision ID: extplatagent01
Revises: auditcontract01
Create Date: 2026-11-09 00:00:00.000000

Scopes a single external-platform instance (one WhatsApp number, one Slack
workspace, …) to a specific set of agents. One row per (platform, data source)
the instance exposes. No rows for an instance means "expose everything the
messaging user can access on this channel" — the pre-existing behavior — so the
table is purely additive and existing connections are untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'extplatagent01'
down_revision: Union[str, None] = 'auditcontract01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'external_platform_agents',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('platform_id', sa.String(length=36), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['platform_id'], ['external_platforms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform_id', 'data_source_id', name='uq_external_platform_agent'),
    )
    op.create_index(
        'ix_external_platform_agents_organization_id',
        'external_platform_agents', ['organization_id'],
    )
    op.create_index(
        'ix_external_platform_agents_platform_id',
        'external_platform_agents', ['platform_id'],
    )
    op.create_index(
        'ix_external_platform_agents_data_source_id',
        'external_platform_agents', ['data_source_id'],
    )
    op.create_index(
        'ix_external_platform_agents_id', 'external_platform_agents', ['id'],
    )


def downgrade() -> None:
    op.drop_index('ix_external_platform_agents_id', table_name='external_platform_agents')
    op.drop_index('ix_external_platform_agents_data_source_id', table_name='external_platform_agents')
    op.drop_index('ix_external_platform_agents_platform_id', table_name='external_platform_agents')
    op.drop_index('ix_external_platform_agents_organization_id', table_name='external_platform_agents')
    op.drop_table('external_platform_agents')
