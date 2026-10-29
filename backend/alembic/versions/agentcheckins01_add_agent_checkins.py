"""add agent_checkins table

Revision ID: agentcheckins01
Revises: artchatmodel01
Create Date: 2026-09-26 00:00:00.000000

One row per agent check-in decision (planned, declined, cancelled, skipped,
ran quietly, sent). Backs the invisible one-shot follow-up feature and the
TraceModal check-in cards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'agentcheckins01'
down_revision: Union[str, None] = 'artchatmodel01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_checkins',
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('source_completion_id', sa.String(length=36), sa.ForeignKey('completions.id'), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('plan_reason', sa.Text(), nullable=True),
        sa.Column('due_at', sa.DateTime(), nullable=True),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('status_reason', sa.String(length=64), nullable=True),
        sa.Column('judge_decision', sa.String(length=8), nullable=True),
        sa.Column('judge_reason', sa.Text(), nullable=True),
        sa.Column('judge_focus', sa.Text(), nullable=True),
        sa.Column('judged_at', sa.DateTime(), nullable=True),
        sa.Column('run_completion_id', sa.String(length=36), sa.ForeignKey('completions.id'), nullable=True),
        sa.Column('notified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('notify_subject', sa.String(length=280), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_agent_checkins_id', 'agent_checkins', ['id'], unique=True)
    op.create_index('ix_agent_checkins_organization_id', 'agent_checkins', ['organization_id'])
    op.create_index('ix_agent_checkins_user_id', 'agent_checkins', ['user_id'])
    op.create_index('ix_agent_checkins_report_id', 'agent_checkins', ['report_id'])
    op.create_index('ix_agent_checkins_source_completion_id', 'agent_checkins', ['source_completion_id'])
    op.create_index('ix_agent_checkins_status', 'agent_checkins', ['status'])
    op.create_index('ix_agent_checkins_org_status', 'agent_checkins', ['organization_id', 'status'])
    op.create_index('ix_agent_checkins_user_status', 'agent_checkins', ['user_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_agent_checkins_user_status', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_org_status', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_status', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_source_completion_id', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_report_id', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_user_id', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_organization_id', table_name='agent_checkins')
    op.drop_index('ix_agent_checkins_id', table_name='agent_checkins')
    op.drop_table('agent_checkins')
