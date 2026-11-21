"""add connections.unreachable_since

Revision ID: connsince01
Revises: rel0629
Create Date: 2026-11-21 20:00:00.000000

A connection the health probes deactivate keeps the time it went down, so
the agent can tell the user how long a source has been unreachable instead
of only that it is.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'connsince01'
down_revision: Union[str, Sequence[str], None] = 'rel0629'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return any(c['name'] == 'unreachable_since' for c in sa.inspect(op.get_bind()).get_columns('connections'))


def upgrade() -> None:
    # Existing rows stay NULL: when a connection that is already down went
    # down is not recorded anywhere, and a guess would be reported as fact.
    if not _has_column():
        op.add_column('connections', sa.Column('unreachable_since', sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table('connections') as batch:
            batch.drop_column('unreachable_since')
