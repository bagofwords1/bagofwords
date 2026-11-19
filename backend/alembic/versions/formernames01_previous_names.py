"""add previous_names to data_sources and connections

Revision ID: formernames01
Revises: worldai01
Create Date: 2026-11-18 12:00:00.000000

Saved step and entity code reaches a source by name
(`ds_clients["<agent>:<connection>"]`), so renaming an agent or a connection
stranded every saved query that spelled the old name. The row now keeps the
names it used to have, and a run resolves an old key to the same client.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'formernames01'
down_revision: Union[str, Sequence[str], None] = 'worldai01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('data_sources', 'connections')


def _has_column(table: str) -> bool:
    return any(c['name'] == 'previous_names' for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    # Idempotent: the dashboards corpus runs schema-only revisions unstamped,
    # so a later `upgrade head` runs this again.
    for table in _TABLES:
        if not _has_column(table):
            op.add_column(table, sa.Column('previous_names', sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        if _has_column(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column('previous_names')
