"""overnight learning: dream_runs, user_open_threads, habit_offers + watermarks

Revision ID: dream01
Revises: mrgckmem01
Create Date: 2026-09-28 10:00:00.000000

Backs the nightly agent dream (per-agent instruction consolidation) and user
dream (memory tidying, open threads, planned check-ins, habit offers), plus the
session-start briefing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'dream01'
down_revision: Union[str, None] = 'mrgckmem01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_cols():
    return [
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'dream_runs',
        *_base_cols(),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id', ondelete='SET NULL'), nullable=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('local_date', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('status_reason', sa.String(length=64), nullable=True),
        sa.Column('inputs_summary', sa.JSON(), nullable=True),
        sa.Column('tool_calls', sa.JSON(), nullable=True),
        sa.Column('outputs', sa.JSON(), nullable=True),
        sa.Column('tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_dream_runs_id', 'dream_runs', ['id'], unique=True)
    op.create_index('ix_dream_runs_organization_id', 'dream_runs', ['organization_id'])
    op.create_index('ix_dream_runs_status', 'dream_runs', ['status'])
    op.create_index('ix_dream_runs_org_date', 'dream_runs', ['organization_id', 'local_date'])
    op.create_index('ix_dream_runs_user_kind', 'dream_runs', ['user_id', 'kind'])
    op.create_index('ix_dream_runs_ds_kind', 'dream_runs', ['data_source_id', 'kind'])

    op.create_table(
        'user_open_threads',
        *_base_cols(),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('text', sa.String(length=200), nullable=False),
        sa.Column('unblocked_by', sa.String(length=200), nullable=True),
        sa.Column('dream_run_id', sa.String(length=36), sa.ForeignKey('dream_runs.id'), nullable=True),
        sa.Column('status', sa.String(length=12), nullable=False),
    )
    op.create_index('ix_user_open_threads_id', 'user_open_threads', ['id'], unique=True)
    op.create_index('ix_user_open_threads_organization_id', 'user_open_threads', ['organization_id'])
    op.create_index('ix_user_open_threads_user_id', 'user_open_threads', ['user_id'])
    op.create_index('ix_user_open_threads_report_id', 'user_open_threads', ['report_id'])
    op.create_index('ix_user_open_threads_org_user_status', 'user_open_threads', ['organization_id', 'user_id', 'status'])

    op.create_table(
        'habit_offers',
        *_base_cols(),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('intent_text', sa.String(length=200), nullable=False),
        sa.Column('cadence', sa.String(length=16), nullable=False),
        sa.Column('suggested_time', sa.String(length=5), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('scheduled_prompt_id', sa.String(length=36), sa.ForeignKey('scheduled_prompts.id'), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('dream_run_id', sa.String(length=36), sa.ForeignKey('dream_runs.id'), nullable=True),
    )
    op.create_index('ix_habit_offers_id', 'habit_offers', ['id'], unique=True)
    op.create_index('ix_habit_offers_organization_id', 'habit_offers', ['organization_id'])
    op.create_index('ix_habit_offers_user_id', 'habit_offers', ['user_id'])
    op.create_index('ix_habit_offers_report_id', 'habit_offers', ['report_id'])
    op.create_index('ix_habit_offers_org_user_status', 'habit_offers', ['organization_id', 'user_id', 'status'])

    op.add_column('memberships', sa.Column('overnight_prep', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('memberships', sa.Column('user_dreamed_at', sa.DateTime(), nullable=True))
    op.add_column('memberships', sa.Column('briefing_seen_at', sa.DateTime(), nullable=True))
    op.add_column('data_sources', sa.Column('agent_dreamed_at', sa.DateTime(), nullable=True))
    op.add_column('agent_checkins', sa.Column('origin', sa.String(length=8), nullable=False, server_default='turn'))
    # Plain column (no FK constraint): SQLite can't add a constrained column
    # without a table rebuild, and the link is informational.
    op.add_column('agent_checkins', sa.Column('dream_run_id', sa.String(length=36), nullable=True))
    op.add_column('agent_checkins', sa.Column('briefing_feedback', sa.String(length=12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agent_checkins') as batch_op:
        batch_op.drop_column('briefing_feedback')
        batch_op.drop_column('dream_run_id')
        batch_op.drop_column('origin')
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.drop_column('agent_dreamed_at')
    with op.batch_alter_table('memberships') as batch_op:
        batch_op.drop_column('briefing_seen_at')
        batch_op.drop_column('user_dreamed_at')
        batch_op.drop_column('overnight_prep')
    op.drop_table('habit_offers')
    op.drop_table('user_open_threads')
    op.drop_table('dream_runs')
