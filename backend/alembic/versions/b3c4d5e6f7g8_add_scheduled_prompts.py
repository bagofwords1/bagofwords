"""add scheduled_prompts table and scheduled_prompt_id to completions

Revision ID: b3c4d5e6f7g8
Revises: a2b3c4d5e6f7
Create Date: 2026-04-04 00:00:00.000000

Adds ScheduledPrompt model for recurring AI prompt execution within reports.
Each scheduled prompt runs on a cron schedule, producing tagged completions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3c4d5e6f7g8'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scheduled_prompts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False, index=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('prompt', sa.JSON(), nullable=False),
        sa.Column('cron_schedule', sa.String(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('notification_subscribers', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime()),
        sa.Column('updated_at', sa.DateTime()),
        sa.Column('deleted_at', sa.DateTime()),
    )

    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scheduled_prompt_id', sa.String(36), nullable=True))
        batch_op.create_index('ix_completions_scheduled_prompt_id', ['scheduled_prompt_id'])


def downgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.drop_index('ix_completions_scheduled_prompt_id')
        batch_op.drop_column('scheduled_prompt_id')

    op.drop_table('scheduled_prompts')
