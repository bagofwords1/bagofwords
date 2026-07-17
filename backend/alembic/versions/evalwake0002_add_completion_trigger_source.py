"""add trigger_source to completions

Revision ID: evalwake0002
Revises: evalwake0001
Create Date: 2026-07-17 00:00:00.000000

Machine-initiated turns (eval run-finished wakes, wait resumes) adopt the
webhook three-completion idiom: a visible role='external' event entry, a
hidden role='user' trigger prompt, and the agent reply. trigger_source marks
the machine origin so the hidden trigger can be filtered from the timeline
without a webhook row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'evalwake0002'
down_revision: Union[str, None] = 'evalwake0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('completions', sa.Column('trigger_source', sa.String(), nullable=True))
    op.create_index('ix_completions_trigger_source', 'completions', ['trigger_source'])


def downgrade() -> None:
    op.drop_index('ix_completions_trigger_source', table_name='completions')
    op.drop_column('completions', 'trigger_source')
