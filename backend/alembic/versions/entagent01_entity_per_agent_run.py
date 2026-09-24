"""saved queries run per agent

Revision ID: entagent01
Revises: pbiidentity01

A saved query (entity) shared with several agents now runs against the agent
it is run from, not against whichever agent its code was written for:

- entities.code_mode / origin_data_source_id: how the code binds to agents
  and the agent it was written for (the default agent and the owner of the
  existing `data` snapshot).
- entity_agent_snapshots: the shared result on each of the query's other
  agents.
- entity_user_results.data_source_id: per-viewer slices are per agent too.

Backfill takes agent names out of stored code (`ds_clients["jtlv:JTLV"]` ->
`ds_clients["$agent:powerbi"]`) where it can do so safely; code it cannot
rewrite is classified (bound / unresolved) and left byte-for-byte as it was.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.migrations.entity_per_agent_v1 import backfill, restore_code


revision: str = 'entagent01'
down_revision: Union[str, None] = 'pbiidentity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('code_mode', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('origin_data_source_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_entities_origin_data_source_id', ['origin_data_source_id'])
        batch_op.create_foreign_key(
            'fk_entities_origin_data_source_id', 'data_sources',
            ['origin_data_source_id'], ['id'], ondelete='SET NULL',
        )

    op.create_table(
        'entity_agent_snapshots',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('entity_id', sa.String(length=36), sa.ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False, index=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('applied_params', sa.JSON(), nullable=True),
        sa.Column('last_refreshed_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='success'),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('entity_id', 'data_source_id', name='uq_entity_agent_snapshots_entity_agent'),
    )

    with op.batch_alter_table('entity_user_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data_source_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_entity_user_results_data_source_id', ['data_source_id'])
        batch_op.create_foreign_key(
            'fk_entity_user_results_data_source_id', 'data_sources',
            ['data_source_id'], ['id'], ondelete='CASCADE',
        )
        batch_op.drop_constraint('uq_entity_user_results_entity_user_params', type_='unique')
        batch_op.create_unique_constraint(
            'uq_entity_user_results_entity_user_agent_params',
            ['entity_id', 'user_id', 'data_source_id', 'params_fingerprint'],
        )

    backfill(op.get_bind())

    # NULLs never collide in the unique key above: an agentless query's slices
    # (data_source_id NULL) stay unique through a partial index.
    op.create_index(
        'uq_entity_user_results_agentless', 'entity_user_results',
        ['entity_id', 'user_id', 'params_fingerprint'], unique=True,
        postgresql_where=sa.text('data_source_id IS NULL'),
        sqlite_where=sa.text('data_source_id IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_entity_user_results_agentless', table_name='entity_user_results')
    bind = op.get_bind()
    restore_code(bind)
    # One slice per (entity, user, values) again: keep the origin agent's.
    bind.execute(sa.text(
        """DELETE FROM entity_user_results WHERE data_source_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM entities e WHERE e.id = entity_user_results.entity_id
               AND e.origin_data_source_id = entity_user_results.data_source_id)"""
    ))
    with op.batch_alter_table('entity_user_results', schema=None) as batch_op:
        batch_op.drop_constraint('uq_entity_user_results_entity_user_agent_params', type_='unique')
        batch_op.create_unique_constraint(
            'uq_entity_user_results_entity_user_params',
            ['entity_id', 'user_id', 'params_fingerprint'],
        )
        batch_op.drop_constraint('fk_entity_user_results_data_source_id', type_='foreignkey')
        batch_op.drop_index('ix_entity_user_results_data_source_id')
        batch_op.drop_column('data_source_id')

    op.drop_table('entity_agent_snapshots')

    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.drop_constraint('fk_entities_origin_data_source_id', type_='foreignkey')
        batch_op.drop_index('ix_entities_origin_data_source_id')
        batch_op.drop_column('origin_data_source_id')
        batch_op.drop_column('code_mode')
