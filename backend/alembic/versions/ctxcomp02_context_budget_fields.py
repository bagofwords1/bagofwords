"""Context budget fields on report_context_states (token anchor, cooldown, last result)

Revision ID: ctxcomp02
Revises: extplatagent01
Create Date: 2026-11-10

Idempotent (each column is added/dropped only if absent/present) so the
dashboard test corpus can apply it unstamped after its data migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ctxcomp02'
down_revision: Union[str, None] = 'extplatagent01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'report_context_states'


def _columns() -> list:
    return [
        sa.Column('last_prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('last_prompt_estimate', sa.Integer(), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cooldown_until', sa.DateTime(), nullable=True),
        sa.Column('last_method', sa.String(length=16), nullable=True),
        sa.Column('degraded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('last_tokens_before', sa.Integer(), nullable=True),
        sa.Column('last_tokens_after', sa.Integer(), nullable=True),
    ]


def _existing() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing()
    missing = [c for c in _columns() if c.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        for col in missing:
            batch.add_column(col)


def downgrade() -> None:
    existing = _existing()
    present = [c.name for c in reversed(_columns()) if c.name in existing]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in present:
            batch.drop_column(name)
