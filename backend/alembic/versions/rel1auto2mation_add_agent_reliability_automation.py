"""add agent reliability automation

Revision ID: rel1auto2mation
Revises: e7f0a1b2c3d4
Create Date: 2026-06-15 09:30:00.000000

Adds the agent-reliability automation feature:

  * ``data_sources.automation_settings`` (JSON, nullable) — per-agent override
    of the org-default automation policy.
  * ``data_sources.reliability_status`` (String, default 'ok') — outcome of the
    automation loop, orthogonal to ``publish_status``.
  * ``agent_automation_runs`` — audit log, one row per loop firing.

Stored as plain JSON/String for clean SQLite/Postgres parity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'rel1auto2mation'
down_revision: Union[str, None] = 'e7f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.add_column(sa.Column('automation_settings', sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'reliability_status',
                sa.String(),
                nullable=False,
                server_default='training',
            )
        )

    op.create_table(
        'agent_automation_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), nullable=False),
        sa.Column('trigger', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('iterations', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('build_id', sa.String(length=36), nullable=True),
        sa.Column('test_run_ids_json', sa.JSON(), nullable=True),
        sa.Column('detail_json', sa.JSON(), nullable=True),
        sa.Column('requested_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['build_id'], ['instruction_builds.id']),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_agent_automation_runs_organization_id',
        'agent_automation_runs', ['organization_id'],
    )
    op.create_index(
        'ix_agent_automation_runs_data_source_id',
        'agent_automation_runs', ['data_source_id'],
    )
    op.create_index(
        'ix_agent_automation_runs_status',
        'agent_automation_runs', ['status'],
    )
    op.create_index(
        'ix_agent_automation_runs_build_id',
        'agent_automation_runs', ['build_id'],
    )
    op.create_index(
        'ix_agent_automation_runs_requested_by_user_id',
        'agent_automation_runs', ['requested_by_user_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_agent_automation_runs_requested_by_user_id', table_name='agent_automation_runs')
    op.drop_index('ix_agent_automation_runs_build_id', table_name='agent_automation_runs')
    op.drop_index('ix_agent_automation_runs_status', table_name='agent_automation_runs')
    op.drop_index('ix_agent_automation_runs_data_source_id', table_name='agent_automation_runs')
    op.drop_index('ix_agent_automation_runs_organization_id', table_name='agent_automation_runs')
    op.drop_table('agent_automation_runs')

    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.drop_column('reliability_status')
        batch_op.drop_column('automation_settings')
