"""add description to instructions and instruction_versions

Revision ID: bd5c1a7e9f02
Revises: judge0001
Create Date: 2026-06-20 00:00:00.000000

Adds an optional, user-authored ``description`` to instructions so long skills
can carry a short blurb that's advertised in the <available_skills> catalog
(instead of falling back to the first line of the text).

The field is versioned: it's snapshotted onto ``instruction_versions`` too, so
edits to it create a new version and participate in build promotion/diffing.

Stored as nullable Text on both tables for clean SQLite/Postgres parity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bd5c1a7e9f02'
down_revision: Union[str, None] = 'judge0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))
    with op.batch_alter_table('instruction_versions') as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('instruction_versions') as batch_op:
        batch_op.drop_column('description')
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.drop_column('description')
