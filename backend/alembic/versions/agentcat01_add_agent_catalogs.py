"""add agent catalogs

Revision ID: agentcat01
Revises: idxactivity01
Create Date: 2026-09-17

Adds the agent_catalogs table (org-level named groupings of agents) and the
nullable data_sources.catalog_id membership FK. Existing agents start
uncatalogued. Catalogs are organizational only — no access semantics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'agentcat01'
down_revision: Union[str, None] = 'idxactivity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_catalogs',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False, unique=True, index=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('color', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_agent_catalogs_organization_id', 'agent_catalogs', ['organization_id'])

    with op.batch_alter_table('data_sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('catalog_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            'fk_data_sources_catalog_id', 'agent_catalogs', ['catalog_id'], ['id'], ondelete='SET NULL'
        )
        batch_op.create_index('ix_data_sources_catalog_id', ['catalog_id'])


def downgrade() -> None:
    with op.batch_alter_table('data_sources', schema=None) as batch_op:
        batch_op.drop_index('ix_data_sources_catalog_id')
        batch_op.drop_constraint('fk_data_sources_catalog_id', type_='foreignkey')
        batch_op.drop_column('catalog_id')

    op.drop_index('ix_agent_catalogs_organization_id', table_name='agent_catalogs')
    op.drop_table('agent_catalogs')
