"""Context clear boundaries on report_context_states (/clear)

Revision ID: ctxclear01
Revises: queryexec03
Create Date: 2026-11-17

`clear_boundaries` lists the completion ids after which the user ran /clear,
oldest first. The newest one is the hard context boundary: nothing at or
before it (not even the protected opening exchange) reaches the agent. The
transcript renders a "Context cleared" divider after each one.

Idempotent (added/dropped only if absent/present), like ctxcomp02.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ctxclear01'
down_revision: Union[str, None] = 'queryexec03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'report_context_states'
COLUMN = 'clear_boundaries'


def _existing() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN in _existing():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    if COLUMN not in _existing():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
