"""add description column to instruction_builds

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9
Create Date: 2026-04-06 00:00:00.000000

Adds a free-text description column to instruction_builds, used as a
"commit message" style rationale. The knowledge harness populates it
by concatenating evidence strings from its create/edit tool calls.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5e6f7g8h9i0'
down_revision: Union[str, None] = 'c4d5e6f7g8h9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instruction_builds', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('instruction_builds', 'description')
