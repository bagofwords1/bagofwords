"""Context budget fields on report_context_states (token anchor, cooldown, last result)

Revision ID: ctxcomp02
Revises: extplatagent01
Create Date: 2026-11-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ctxcomp02'
down_revision: Union[str, None] = 'extplatagent01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('report_context_states') as batch:
        batch.add_column(sa.Column('last_prompt_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('last_prompt_estimate', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('cooldown_until', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('last_method', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('degraded', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('last_tokens_before', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('last_tokens_after', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('report_context_states') as batch:
        for col in (
            'last_tokens_after', 'last_tokens_before', 'degraded', 'last_method',
            'cooldown_until', 'failure_count', 'last_prompt_estimate', 'last_prompt_tokens',
        ):
            batch.drop_column(col)
