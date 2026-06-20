"""add kind to instructions

Revision ID: e7f0a1b2c3d4
Revises: d6d9a78b7b4a
Create Date: 2026-06-15 00:00:00.000000

Adds the ``kind`` column to instructions so an instruction can be either a
normal instruction or a "skill".

Values:
  - instruction — a normal instruction (default)
  - skill       — a skill

Stored as a plain string (not a DB enum) for clean SQLite/Postgres parity.
Existing rows are backfilled to ``instruction`` via the server default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f0a1b2c3d4'
down_revision: Union[str, None] = 'd6d9a78b7b4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.add_column(
            sa.Column(
                'kind',
                sa.String(length=50),
                nullable=False,
                server_default='instruction',
            )
        )
    # Backfill existing rows explicitly (server_default already covers this,
    # but be explicit for clarity and any pre-existing NULLs).
    op.execute("UPDATE instructions SET kind = 'instruction' WHERE kind IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.drop_column('kind')
