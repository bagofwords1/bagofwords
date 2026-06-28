"""add prompt_runs table

Usage tracking + audit + run-for provenance for saved prompts.
  id, prompt_id, user_id (report owner / whose run), actor_id (who triggered),
  report_id (nullable), parameters JSON, + BaseSchema timestamps.

Revision ID: c1d2e3f4a5b6
Revises: f7a8b9c0d1e2
Create Date: 2026-06-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'prompt_runs' in insp.get_table_names():
        return
    op.create_table(
        'prompt_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('prompt_id', sa.String(length=36), sa.ForeignKey('prompts.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('actor_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=True),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_prompt_runs_id', 'prompt_runs', ['id'], unique=True)
    op.create_index('ix_prompt_runs_prompt_id', 'prompt_runs', ['prompt_id'])
    op.create_index('ix_prompt_runs_user_id', 'prompt_runs', ['user_id'])
    op.create_index('ix_prompt_runs_actor_id', 'prompt_runs', ['actor_id'])
    op.create_index('ix_prompt_runs_report_id', 'prompt_runs', ['report_id'])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'prompt_runs' in insp.get_table_names():
        op.drop_table('prompt_runs')
