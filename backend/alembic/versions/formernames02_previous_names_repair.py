"""repair: add previous_names where rel0629 was applied before it covered formernames01

Revision ID: formernames02
Revises: rel0629
Create Date: 2026-11-19 06:30:00.000000

rel0629 first shipped merging three heads; formernames01 was added to its
parents a few hours later. A database upgraded in between is stamped rel0629,
so alembic considers formernames01 applied and never runs it, and the backend
fails at startup with `column data_sources.previous_names does not exist`.
This revision sits after rel0629 and adds the columns wherever they are
missing; it is a no-op on databases that ran formernames01.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'formernames02'
down_revision: Union[str, Sequence[str], None] = 'rel0629'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('data_sources', 'connections')


def _has_column(table: str) -> bool:
    return any(c['name'] == 'previous_names' for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    for table in _TABLES:
        if not _has_column(table):
            op.add_column(table, sa.Column('previous_names', sa.JSON(), nullable=True))


def downgrade() -> None:
    # formernames01 owns the columns; dropping them here would leave a database
    # stamped rel0629 without columns that revision's parents promise.
    pass
