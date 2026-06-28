"""add INSTRUCTION value to mentiontype enum

Revision ID: f7a8b9c0d1e2
Revises: 6d171618ced8
Create Date: 2026-06-27 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = '6d171618ced8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the INSTRUCTION value to the mentiontype enum for PostgreSQL.
    # SQLite stores the column as VARCHAR (no native enum), so this is a no-op
    # there. `ALTER TYPE ... ADD VALUE` cannot run inside a transaction block,
    # so use an autocommit block (Alembic wraps migrations in a transaction).
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE mentiontype ADD VALUE IF NOT EXISTS 'INSTRUCTION'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type, and SQLite
    # stores the column as VARCHAR. Nothing to reverse.
    pass
