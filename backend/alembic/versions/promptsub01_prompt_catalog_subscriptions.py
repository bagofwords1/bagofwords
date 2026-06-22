"""prompt catalog + scheduled prompt subscriptions

Extends the existing `prompts` table into an org catalog + execution spec,
adds a prompt<->data_source M2M, extends `scheduled_prompts` with catalog
linkage + delivery channel + run mode, and adds `reports.source_scheduled_prompt_id`.

Revision ID: promptsub01
Revises: bd5c1a7e9f02
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'promptsub01'
down_revision: Union[str, None] = 'bd5c1a7e9f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(insp, table: str, column: str) -> bool:
    try:
        return column in {c['name'] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # ── prompts: catalog + execution spec columns ──
    prompt_cols = [
        sa.Column('mode', sa.String(), nullable=False, server_default='chat'),
        sa.Column('model_id', sa.String(length=36), nullable=True),
        sa.Column('mentions', sa.JSON(), nullable=True),
        sa.Column('scope', sa.String(), nullable=False, server_default='private'),
        sa.Column('is_starter', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.String(), nullable=False, server_default='draft'),
        sa.Column('default_cron', sa.String(), nullable=True),
        sa.Column('default_channel', sa.String(), nullable=True),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
    ]
    for col in prompt_cols:
        if not _has_column(insp, 'prompts', col.name):
            op.add_column('prompts', col)

    # ── scheduled_prompts: subscription linkage + delivery ──
    sp_cols = [
        sa.Column('prompt_id', sa.String(length=36), nullable=True),
        sa.Column('channel', sa.String(), nullable=True),
        sa.Column('run_mode', sa.String(), nullable=False, server_default='append'),
        sa.Column('created_by', sa.String(length=36), nullable=True),
    ]
    for col in sp_cols:
        if not _has_column(insp, 'scheduled_prompts', col.name):
            op.add_column('scheduled_prompts', col)
    try:
        op.create_index('ix_scheduled_prompts_prompt_id', 'scheduled_prompts', ['prompt_id'])
    except Exception:
        pass

    # ── reports: group new_report runs under their task ──
    if not _has_column(insp, 'reports', 'source_scheduled_prompt_id'):
        op.add_column('reports', sa.Column('source_scheduled_prompt_id', sa.String(length=36), nullable=True))
        try:
            op.create_index('ix_reports_source_scheduled_prompt_id', 'reports', ['source_scheduled_prompt_id'])
        except Exception:
            pass

    # ── prompt <-> data_source M2M ──
    if 'prompt_data_source_association' not in insp.get_table_names():
        op.create_table(
            'prompt_data_source_association',
            sa.Column('prompt_id', sa.String(length=36), sa.ForeignKey('prompts.id'), nullable=True),
            sa.Column('data_source_id', sa.String(length=36), sa.ForeignKey('data_sources.id'), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if 'prompt_data_source_association' in insp.get_table_names():
        op.drop_table('prompt_data_source_association')

    for idx, table in [
        ('ix_reports_source_scheduled_prompt_id', 'reports'),
        ('ix_scheduled_prompts_prompt_id', 'scheduled_prompts'),
    ]:
        try:
            op.drop_index(idx, table_name=table)
        except Exception:
            pass

    for col in ['source_scheduled_prompt_id']:
        if _has_column(insp, 'reports', col):
            op.drop_column('reports', col)
    for col in ['prompt_id', 'channel', 'run_mode', 'created_by']:
        if _has_column(insp, 'scheduled_prompts', col):
            op.drop_column('scheduled_prompts', col)
    for col in ['mode', 'model_id', 'mentions', 'scope', 'is_starter', 'status',
                'default_cron', 'default_channel', 'category', 'tags']:
        if _has_column(insp, 'prompts', col):
            op.drop_column('prompts', col)
